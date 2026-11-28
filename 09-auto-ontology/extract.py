"""36장. 업무 문서·상담 로그에서 규칙과 Action 추출 → MiniOnto DSL 변환 → 검증기로 거르기

파이프라인
  1. 분할        문서를 절(## 제목) 단위로 나눠 묶는다 (한 덩어리 최대 MAX_CHARS 자)
  2. 추출        덩어리마다 LLM → JSON {rules, actions}  (객체 모델·역할 목록을 함께 준다)
  3. 합치기      덩어리별 결과의 중복을 임베딩 유사도로 합친다
  4. DSL 변환    Action 후보마다 LLM → MiniOnto Action 정의(JSON) + 상태 전이
  5. 검증·수리   초안 정의를 검증기로 검사하고, 오류가 난 Action 은 오류 문장을 주고 다시 쓰게 한다 (최대 2번)

객체 모델(객체·속성·링크·Function·역할·상태 값)은 이미 있다고 본다 — 세무 정의(taxos)의 것을 쓴다.
Action 과 상태 전이만 문서에서 만든다. 예시는 다른 도메인(Northwind·설비 정비)의 Action 을 보여 준다.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENGINE = ROOT / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto.metamodel import Ontology  # noqa: E402
from minionto.validator import validate  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
SOURCES = HERE / "sources"
GOLD = HERE / "gold" / "gold.yaml"
MAX_CHARS = 1800
KINDS = ["실행조건", "권한", "승인", "상태전이", "정책", "DB제약", "멱등"]


# ---------------------------------------------------------------------- 1. 분할
@dataclass
class Chunk:
    source: str              # manual | logs
    title: str
    text: str


def split_sections(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"^## ", text, flags=re.M)
    return [(p.splitlines()[0].strip(), "## " + p.strip()) for p in parts[1:]]


def chunks(include_logs: bool = True) -> list[Chunk]:
    out: list[Chunk] = []
    for name, src in (("manual.md", "manual"), ("consult_logs.md", "logs")):
        if src == "logs" and not include_logs:
            continue
        buf: list[str] = []
        titles: list[str] = []
        for title, body in split_sections((SOURCES / name).read_text(encoding="utf-8")):
            if buf and sum(map(len, buf)) + len(body) > MAX_CHARS:
                out.append(Chunk(src, " / ".join(titles), "\n\n".join(buf)))
                buf, titles = [], []
            buf.append(body)
            titles.append(title)
        if buf:
            out.append(Chunk(src, " / ".join(titles), "\n\n".join(buf)))
    return out


# ---------------------------------------------------------------------- 객체 모델 설명
def object_model(onto: Ontology, scope: set[str]) -> str:
    """LLM 에게 줄 객체 모델: 객체·속성(타입, 허용 값)·링크·상태·Function·역할 (Action 은 빼고)"""
    from minionto.schema import Schema
    sch = Schema(onto)
    lines = [f"역할: {', '.join(onto.roles)}", "실행자 유형: human(사람), agent(AI 에이전트), system(시스템)", "", "# 객체"]
    for name, ot in onto.object_types.items():
        if name not in scope:
            continue
        lines.append(f"{name} ({ot.display_name}) — 기본키 {ot.source.primary_key}")
        lines.append("  속성 " + ", ".join(
            f"{p}:{d.type}" + (f"[{'|'.join(d.values)}]" if d.values else "") for p, d in ot.properties.items()))
        links = [f"{e.name}→{e.target}{'[]' if e.many else ''}" for e in sch.links[name].values() if e.target in scope]
        if links:
            lines.append("  링크 " + ", ".join(links))
        if ot.state_machine:
            sm = ot.state_machine
            lines.append(f"  상태 속성 {sm.property} (처음 {sm.initial}, 끝 {sm.terminal})")
    lines += ["", "# Function (조건에 {function: 이름, args: {입력: 값}} 으로 쓴다)"]
    for n, f in onto.functions.items():
        lines.append(f"{n}({', '.join(f'{k}:{v.object_type or v.type}' for k, v in f.inputs.items())}) → "
                     f"{f.returns.type} — {f.description or ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- LLM · 임베딩
class LLM:
    def __init__(self):
        from minionto.llm import ChatClient
        self.chat = ChatClient()
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def json(self, system: str, user: str) -> dict:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last = None
        for _ in range(2):                          # JSON 이 깨지면 한 번 더
            r = self.chat.chat(msgs, response_format={"type": "json_object"})
            self.usage["calls"] += 1
            for k in ("prompt_tokens", "completion_tokens"):
                self.usage[k] += r.get("usage", {}).get(k, 0)
            text = r["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{.*\}", text, re.S)
            try:
                return json.loads(m.group(0) if m else text)
            except json.JSONDecodeError as e:
                last = e
        raise ValueError(f"LLM 응답이 JSON 이 아닙니다: {last}")


class Embedder:
    def __init__(self):
        self.base = (os.getenv("EMBED_BASE_URL") or "").rstrip("/")
        self.model = os.getenv("EMBED_SERVER_MODEL") or os.getenv("EMBED_MODEL") or ""
        if not (self.base and self.model):
            raise RuntimeError("임베딩 API(EMBED_BASE_URL, EMBED_SERVER_MODEL)가 필요합니다")
        self.cache: dict[str, list[float]] = {}

    def __call__(self, texts: list[str]) -> list[list[float]]:
        new = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if new:
            url = self.base + ("/embeddings" if self.base.endswith("/v1") else "/v1/embeddings")
            r = httpx.post(url, json={"model": self.model, "input": new}, timeout=120)
            r.raise_for_status()
            for t, d in zip(new, sorted(r.json()["data"], key=lambda x: x["index"])):
                self.cache[t] = d["embedding"]
        return [self.cache[t] for t in texts]


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# ---------------------------------------------------------------------- 2. 추출
EXTRACT_SYSTEM = """너는 업무 문서에서 운영 규칙과 업무 실행 단위(Action)를 뽑는 분석가다. 결과는 JSON 하나로만 답한다.
- 문서에 실제로 쓰인 것만 뽑는다. 추측하거나 일반 상식으로 채우지 않는다. 사무실 안내·요령 같은 비업무 내용은 뺀다.
- rules: 업무 규칙 하나당 항목 하나. 한 문장으로 짧게 다시 쓴다.
  kind 는 다음 중 하나: 실행조건(이럴 때만 할 수 있다), 권한(누가 할 수 있다), 승인(누구의 승인 뒤 실행), 상태전이(상태가 바뀌는 순서),
  정책(누가 무엇을 볼 수 있다), DB제약(데이터가 항상 지켜야 할 조건), 멱등(같은 요청의 중복 실행)
  action 은 그 규칙이 걸리는 업무 이름(actions 의 name 과 같게), 특정 업무에 걸리지 않으면 null. evidence 는 근거 문장 인용.
- actions: 사람이나 AI 가 시스템에서 실행하는 업무(데이터를 바꾸는 일)만. 조회만 하는 일은 빼라.
  name(한국어 업무 이름), target(아래 객체 중 하나), description, roles(할 수 있는 역할), actor_types(human/agent/system),
  preconditions(실행 조건 문장 목록), approval(승인 규칙 문장, 없으면 null), transition({from: [상태], to: 상태} 또는 null),
  effects(실행하면 바뀌는 것 문장 목록)
형식: {"rules": [{"statement", "kind", "action", "evidence"}], "actions": [{...}]}"""


def extract(llm: LLM, chunk: Chunk, model_text: str) -> dict:
    user = f"# 객체 모델 (이미 있는 것)\n{model_text}\n\n# 문서 ({chunk.source}: {chunk.title})\n{chunk.text}"
    out = llm.json(EXTRACT_SYSTEM, user)
    for r in out.get("rules", []):
        r["source"] = chunk.source
    for a in out.get("actions", []):
        a["source"] = chunk.source
    return out


# ---------------------------------------------------------------------- 3. 합치기
def merge(emb: Embedder, results: list[dict], rule_sim: float = 0.92, action_sim: float = 0.85) -> dict:
    rules: list[dict] = []
    for res in results:
        for r in res.get("rules", []):
            if r.get("statement"):
                rules.append(r)
    keep: list[dict] = []
    vecs = emb([r["statement"] for r in rules]) if rules else []
    kv: list[list[float]] = []
    for r, v in zip(rules, vecs):
        j = next((i for i, w in enumerate(kv) if cos(v, w) >= rule_sim), None)
        if j is None:
            keep.append(r)
            kv.append(v)
        elif r["source"] != keep[j]["source"]:
            keep[j]["source"] = "both"
    actions: list[dict] = []
    av: list[list[float]] = []
    for res in results:
        for a in res.get("actions", []):
            if not a.get("name"):
                continue
            v = emb([a["name"]])[0]
            j = next((i for i, (b, w) in enumerate(zip(actions, av))
                      if b.get("target") == a.get("target") and cos(v, w) >= action_sim), None)
            if j is None:
                actions.append(copy.deepcopy(a))
                av.append(v)
                continue
            b = actions[j]
            for k in ("roles", "actor_types", "preconditions", "effects"):
                b[k] = list(dict.fromkeys((b.get(k) or []) + (a.get(k) or [])))
            for k in ("approval", "transition", "description"):
                b[k] = b.get(k) or a.get(k)
            if a["source"] != b["source"]:
                b["source"] = "both"
    return {"rules": keep, "actions": actions}


# ---------------------------------------------------------------------- 4. DSL 변환
DSL_GUIDE = """MiniOnto Action 정의 문법 (JSON 으로 쓴다)
{ "display_name": 한국어 이름, "description": 설명, "target": 객체 타입,
  "parameters": { 이름: {"type": string|integer|decimal|boolean|date|enum|object, "values": [enum 값], "object_type": 객체, "required": true|false, "description": ...} },
  "preconditions": [ {"message": 지키지 않았을 때 보여 줄 문장, "check": 조건식} ],
  "permission": {"roles": [역할], "actor_types": [human|agent|system], "check": 조건식(선택)},
  "approval": {"required_if": 조건식, "approver_roles": [역할], "reason": 문장, "expires_in": "24h"} 또는 생략,
  "edits": [ {"update": "target", "set": {속성: 값}},
             {"update_linked": 링크이름, "where": 조건식(선택), "set": {속성: 값}},
             {"create": 객체 타입, "values": {속성: 값}} ],
  "emits": [이벤트 이름] }
조건식: {"field": 속성 또는 링크.속성, "op": eq|ne|in|not_in|gt|gte|lt|lte|is_null|not_null, "value": 값}
        {"all": [...]}, {"any": [...]}, {"not": {...}}
        {"some": {"link": 여러 개 링크, "where": 조건식}}, {"none": {"link": ..., "where": ...}}  (하나도 없으면 none 이 참)
        {"function": 이름, "args": {입력: 값}}
값 참조: $target.속성, $params.이름, $actor.id|role|type, $today, $now  ($params.이름.속성 처럼 더 들어가지 않는다)
상태 속성을 바꾸는 Action 은 transitions 에 [{"object_type": 객체, "from": [지금 상태], "to": 바뀔 상태}] 를 준다.
대상 객체뿐 아니라 update_linked 로 상태를 바꾸는 연결 객체도 하나씩 적는다 (각 객체의 상태 머신에 이 Action 의 전이로 등록된다).
상태 머신에 등록되지 않은 상태 변경은 검증기와 엔진이 막는다."""

DSL_SYSTEM = """너는 업무 설명을 MiniOnto 온톨로지의 Action 정의로 바꾸는 엔지니어다. 결과는 JSON 하나로만 답한다.
- 객체 모델에 있는 객체·속성·링크·Function·역할·허용 값만 쓴다. 없는 이름을 지어내지 않는다.
- 업무 설명과 관련 규칙에 있는 조건·권한·승인만 넣는다. 설명에 없는 조건을 덧붙이지 않는다.
- 형식: {"name": 영어 snake_case 이름, "definition": Action 정의, "transitions": [{"object_type", "from", "to"}] 또는 []}"""


def examples() -> str:
    """다른 도메인의 Action 예시 2개 (세무 정답이 섞이지 않게)"""
    nw = yaml.safe_load((ENGINE / "ontologies/northwind/ontology.yaml").read_text(encoding="utf-8"))
    mt = yaml.safe_load((ENGINE / "ontologies/maintenance/ontology.yaml").read_text(encoding="utf-8"))
    ex = {"request_purchase": nw["actions"]["request_purchase"], "complete_work": mt["actions"]["complete_work"]}
    return json.dumps(ex, ensure_ascii=False, indent=1)


def to_dsl(llm: LLM, action: dict, rules: list[dict], model_text: str) -> dict:
    related = [r for r in rules if r.get("action") == action["name"]]
    cand = {k: action.get(k) for k in ("name", "target", "description", "roles", "actor_types", "preconditions",
                                       "approval", "transition", "effects")}
    user = (f"{DSL_GUIDE}\n\n# 다른 도메인의 예시\n{examples()}\n\n# 객체 모델\n{model_text}\n\n"
            f"# 바꿀 업무\n{json.dumps(cand, ensure_ascii=False, indent=1)}\n\n"
            f"# 이 업무에 걸린 규칙\n" + "\n".join(f"- [{r['kind']}] {r['statement']}" for r in related))
    return llm.json(DSL_SYSTEM, user)


def repair(llm: LLM, out: dict, errors: list[str], model_text: str) -> dict:
    user = (f"{DSL_GUIDE}\n\n# 객체 모델\n{model_text}\n\n# 지난번 정의\n{json.dumps(out, ensure_ascii=False, indent=1)}\n\n"
            "# 검증기 오류 (모두 고쳐라. 고칠 수 없는 조건은 빼고 description 에 적어라)\n" + "\n".join(f"- {e}" for e in errors))
    return llm.json(DSL_SYSTEM, user)


# ---------------------------------------------------------------------- 5. 초안 정의와 검증
@dataclass
class Draft:
    data: dict                                    # 초안 정의 (YAML 구조)
    actions: dict[str, dict] = field(default_factory=dict)        # 이름 → {definition, transitions, candidate}
    errors: dict[str, list[str]] = field(default_factory=dict)    # 이름 → 검증 오류
    rounds: list[dict] = field(default_factory=list)              # 수리 단계별 {valid, invalid}


def base_definition(gold_actions: set[str]) -> dict:
    """세무 정의에서 정답 Action 과 그 상태 전이·구독을 뺀 것 (객체 모델은 그대로)"""
    data = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
    data["actions"] = {k: v for k, v in data["actions"].items() if k not in gold_actions}
    data["subscriptions"] = {}
    for ot in data["object_types"].values():
        sm = ot.get("state_machine")
        if sm:
            sm["transitions"] = {k: v for k, v in sm["transitions"].items() if k not in gold_actions}
    return data


def transitions_of(entry: dict) -> list[dict]:
    """초안의 상태 전이 목록 [{object_type, from, to}] — 예전 형식(대상 객체 하나의 transition)도 받는다"""
    t = entry.get("transitions", entry.get("transition"))
    target = entry["definition"].get("target") if isinstance(entry.get("definition"), dict) else None
    if isinstance(t, dict):
        t = [{"object_type": target, **t}]
    return [x for x in (t or []) if isinstance(x, dict) and x.get("to")]


def assemble(base: dict, actions: dict[str, dict]) -> dict:
    data = copy.deepcopy(base)
    for name, a in actions.items():
        data["actions"][name] = a["definition"]
        for t in transitions_of(a):
            ot = data["object_types"].get(t.get("object_type"))
            if ot and ot.get("state_machine"):
                frm = t.get("from")
                ot["state_machine"]["transitions"][name] = {"from": frm if isinstance(frm, list) else [frm],
                                                            "to": t["to"]}
    return data


def check(data: dict, names: list[str]) -> dict[str, list[str]]:
    """초안 전체를 검증하고 오류를 Action 별로 나눈다. 문법 오류(Pydantic)는 Action 하나씩 넣어 가며 찾는다"""
    errs: dict[str, list[str]] = {n: [] for n in names}
    try:
        onto = Ontology.model_validate(data)
    except Exception:                               # 문법 오류 — 어느 Action 인지 하나씩 확인
        for n in names:
            single = copy.deepcopy(data)
            single["actions"] = {k: v for k, v in data["actions"].items() if k not in names or k == n}
            for ot in single["object_types"].values():
                sm = ot.get("state_machine")
                if sm:
                    sm["transitions"] = {k: v for k, v in sm["transitions"].items() if k not in names or k == n}
            try:
                Ontology.model_validate(single)
            except Exception as e:                  # noqa: BLE001
                errs[n].append(f"문법: {str(e).splitlines()[0]} — " + " / ".join(
                    ln.strip() for ln in str(e).splitlines()[1:6] if ln.strip()))
        bad = [n for n, v in errs.items() if v]
        good = {k: v for k, v in data["actions"].items() if k not in bad}
        data = copy.deepcopy(data)
        data["actions"] = good
        for ot in data["object_types"].values():
            sm = ot.get("state_machine")
            if sm:
                sm["transitions"] = {k: v for k, v in sm["transitions"].items() if k not in bad}
        onto = Ontology.model_validate(data)
    rep = validate(onto, TAXOS.parent)
    for i in rep.issues:
        if i.level != "error":
            continue
        for n in names:
            if f"actions.{n}." in i.path + "." or f"transitions.{n}" in i.path:
                errs[n].append(f"{i.path}: {i.message}")
                break
    return errs


def build(llm: LLM, merged: dict, model_text: str, gold_actions: set[str], max_repair: int = 2) -> Draft:
    base = base_definition(gold_actions)
    acts: dict[str, dict] = {}
    for cand in merged["actions"]:
        out = to_dsl(llm, cand, merged["rules"], model_text)
        name = re.sub(r"[^a-z0-9_]", "_", str(out.get("name") or "action").lower()).strip("_") or "action"
        while name in acts or name in base["actions"]:
            name += "_2"
        acts[name] = {"definition": out.get("definition") or {},
                      "transitions": out.get("transitions", out.get("transition")), "candidate": cand}
    d = Draft(data={}, actions=acts)
    for rnd in range(max_repair + 1):
        errs = check(assemble(base, acts), list(acts))
        d.rounds.append({"valid": sum(1 for v in errs.values() if not v), "invalid": sum(1 for v in errs.values() if v),
                         "errors": {n: v for n, v in errs.items() if v}})
        d.errors = errs
        bad = [n for n, v in errs.items() if v]
        if not bad or rnd == max_repair:
            break
        for n in bad:
            fixed = repair(llm, {"name": n, "definition": acts[n]["definition"],
                                 "transitions": acts[n]["transitions"]}, errs[n], model_text)
            acts[n]["definition"] = fixed.get("definition") or acts[n]["definition"]
            acts[n]["transitions"] = fixed.get("transitions", fixed.get("transition", acts[n]["transitions"]))
    good = {n: a for n, a in acts.items() if not d.errors.get(n)}
    d.data = assemble(base, good)
    return d


__all__ = ["transitions_of", "assemble", "check", "Chunk", "chunks", "object_model", "LLM", "Embedder", "cos", "extract", "merge", "build", "Draft",
           "base_definition", "KINDS", "GOLD", "TAXOS"]
