"""37장. 전문가 검증과 등록 관문 — 검토 기록 적용, 샌드박스 실행, Q/A 테스트, Registry 등록 관문

    review = load_review("review/review_1.yaml")
    entries, qa, log = apply_review(entries, qa, review)          # 고친 내용은 모두 log 에 남는다
    report = gate(data, entries, qa, review, client, active)      # 여섯 가지 검사
    if report.ok:
        registry.register(Package.from_text(...), actor=review.reviewer, notes=report.notes())

관문 (모두 통과해야 등록한다)
  1. 검증기       정의 문법·참조·상태 전이 오류 0
  2. 검토 완료    생성된 Action 마다 전문가 결정(approve·modify)이 있고, 문서에 답이 없던 Q/A 에 전문가 답이 있다
  3. 변경 범위    현재 활성 버전과의 차이가 검토한 Action 안에만 있다. breaking 변경은 검토 기록에서 허용해야 한다
  4. DB 대조      정의가 실제 테이블·컬럼과 맞는다
  5. 샌드박스     Action 마다 실행할 수 있는 대상에서 실제로 실행해 본다 (항상 롤백). 실행 오류 0
  6. Q/A 테스트   전문가가 확인한 Q/A 로 만든 판단 테스트가 모두 통과한다
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from extract import TAXOS, assemble, check
from qa import ACTOR_OF, build_tests, run_tests, summarize, task_map, test_params

from minionto import OntologyClient
from minionto.metamodel import Ontology
from minionto.registry import diff
from minionto.validator import validate

DECISIONS = ("approve", "modify", "keep_active", "reject")   # keep_active: 생성안 대신 현재 활성 정의를 쓴다


# ---------------------------------------------------------------------- 검토 기록
@dataclass
class Review:
    reviewer: dict
    qa: dict[str, dict] = field(default_factory=dict)          # Q/A 번호 → {answer, expect, case 일부, reason}
    actions: dict[str, dict] = field(default_factory=dict)     # Action 이름 → {decision, rename, patches, reason}
    allow_breaking: str | None = None                          # breaking 변경을 받아들이는 이유


def load_review(path: str | Path) -> Review:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Review(raw["reviewer"], raw.get("qa") or {}, raw.get("actions") or {}, raw.get("allow_breaking"))


def _walk(obj: Any, path: str) -> tuple[Any, Any]:
    """'definition.permission.roles' → (부모, 마지막 키). 숫자는 목록 위치"""
    *head, last = path.split(".")
    for k in head:
        obj = obj[int(k)] if isinstance(obj, list) else obj.setdefault(k, {})
    return obj, (int(last) if isinstance(obj, list) else last)


def patch(entry: dict, op: dict) -> tuple[Any, Any]:
    """op = {set|remove|append: 경로, value: 값} 하나를 적용하고 (바꾸기 전, 바꾼 뒤) 를 돌려준다"""
    kind = next(k for k in ("set", "remove", "append") if k in op)
    parent, key = _walk(entry, op[kind])
    before = copy.deepcopy(parent[key]) if (isinstance(parent, list) and key < len(parent)) or \
        (isinstance(parent, dict) and key in parent) else None
    if kind == "set":
        parent[key] = copy.deepcopy(op["value"])
    elif kind == "remove":
        del parent[key]
    else:
        parent.setdefault(key, []).append(copy.deepcopy(op["value"]))
    return before, None if kind == "remove" else copy.deepcopy(parent[key])


def apply_review(entries: dict[str, dict], qa: list[dict], review: Review) -> tuple[dict, list[dict], list[dict]]:
    """검토 기록을 초안에 적용한다. 원본은 그대로 두고, 바뀐 내용은 log 에 한 줄씩 남긴다"""
    entries, qa = copy.deepcopy(entries), copy.deepcopy(qa)
    by = review.reviewer.get("id")
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log: list[dict] = []
    qa_by_id = {q["id"]: q for q in qa}
    for qid, fix in review.qa.items():
        q = qa_by_id.get(qid)
        if q is None:
            raise KeyError(f"검토 기록의 Q/A {qid} 가 없습니다")
        before = {"answer": q.get("answer"), "case": copy.deepcopy(q.get("case"))}
        if "answer" in fix:
            q["answer"] = fix["answer"]
        q["case"] = {**(q.get("case") or {}), **(fix.get("case") or {})}
        q["grounded"] = fix.get("verdict") != "out_of_scope"
        q["reviewed"] = fix.get("verdict", "fix")
        log.append({"target": f"qa:{qid}", "change": fix.get("verdict", "fix"), "before": before,
                    "after": {"answer": q["answer"], "case": q["case"]}, "reason": fix.get("reason"), "by": by, "at": at})
    out: dict[str, dict] = {}
    for name, e in entries.items():
        d = review.actions.get(name)
        if d is None:                                   # 검토하지 않은 Action — 관문 2에서 걸린다
            out[name] = {**e, "decision": None}
            continue
        if d.get("decision") not in DECISIONS:
            raise ValueError(f"{name}: decision 은 {DECISIONS} 중 하나여야 합니다")
        if d["decision"] == "reject":
            log.append({"target": f"action:{name}", "change": "reject", "reason": d.get("reason"), "by": by, "at": at})
            continue
        new = d.get("rename") or name
        if d["decision"] == "keep_active":
            e = {**active_entry(new), "candidate": e.get("candidate"), "decision": "keep_active"}
            log.append({"target": f"action:{name}", "change": "keep_active", "after": new, "reason": d.get("reason"),
                        "by": by, "at": at})
            out[new] = e
            continue
        e = {**e, "decision": d["decision"]}
        for op in d.get("patches") or []:
            before, after = patch(e, op)
            path = next(op[k] for k in ("set", "remove", "append") if k in op)
            log.append({"target": f"action:{name}", "change": next(k for k in ("set", "remove", "append") if k in op),
                        "path": path, "before": before, "after": after, "reason": op.get("reason") or d.get("reason"),
                        "by": by, "at": at})
        if new != name:
            log.append({"target": f"action:{name}", "change": "rename", "before": name, "after": new,
                        "reason": d.get("reason"), "by": by, "at": at})
        if d["decision"] == "approve":
            log.append({"target": f"action:{name}", "change": "approve", "reason": d.get("reason"), "by": by, "at": at})
        out[new] = e
    return out, qa, log


def active_entry(name: str, path: Path = TAXOS) -> dict:
    """현재 손으로 쓴 정의(활성 버전)의 Action 하나를 초안 형식으로"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if name not in raw["actions"]:
        raise KeyError(f"활성 정의에 Action '{name}' 이 없습니다 (keep_active 는 같은 이름이 있어야 한다)")
    trans = [{"object_type": ot, "from": sm["transitions"][name]["from"], "to": sm["transitions"][name]["to"]}
             for ot, o in raw["object_types"].items()
             if (sm := o.get("state_machine")) and name in sm.get("transitions", {})]
    return {"definition": raw["actions"][name], "transitions": trans}


def renamed(review: Review) -> dict[str, str]:
    return {n: d["rename"] for n, d in review.actions.items() if d.get("rename")}


# ---------------------------------------------------------------------- 관문
@dataclass
class Check:
    name: str
    ok: bool
    details: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


@dataclass
class GateReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def notes(self) -> str:
        return " / ".join(f"{c.name} {'통과' if c.ok else '실패'}" + (f" {c.info}" if c.info else "")
                          for c in self.checks)

    def show(self) -> None:
        for c in self.checks:
            print(f"  [{'통과' if c.ok else '실패'}] {c.name}" + (f"  {c.info}" if c.info else ""))
            for d in c.details[:6]:
                print(f"         - {d}")
            if len(c.details) > 6:
                print(f"         … 외 {len(c.details) - 6}건")
        print(f"  → 관문 {'통과: 등록할 수 있습니다' if self.ok else '실패: 등록하지 않습니다'}")


def _owned(path: str, names: set[str]) -> bool:
    """registry.diff 의 변경 경로가 검토한 Action 의 것인가"""
    parts = path.replace("[", ".").replace("]", "").split(".")
    return any(p in names for p in parts)


def sandbox(c: OntologyClient, names: list[str], params_of, actors: list[dict], n: int = 30) -> Check:
    """Action 마다 실행할 수 있는(ready) 대상 하나를 찾아 실제로 실행하고 롤백한다. 실행 오류가 없어야 통과"""
    ran, errors, idle = [], [], []
    for name in names:
        act = c.onto.actions[name]
        pk = c.onto.object_types[act.target].source.primary_key
        hit = None
        for o in c.objects(act.target).order_by(pk).limit(n).all():
            a = next((a for a in actors if c.actions.check(name, o.pk, actor=a, params=params_of(name)).status == "ready"),
                     None)
            if a:
                hit = (o.pk, a)
                break
        if hit is None:
            idle.append(name)
            continue
        try:
            with c.conn.transaction(force_rollback=True):
                r = c.actions.apply(name, hit[0], actor=hit[1], params=params_of(name))
            if r.status != "completed":
                errors.append(f"{name}({hit[0]}): {r.status} {r.messages[:1]}")
            else:
                ran.append(f"{name}({hit[0]}, {hit[1]['role']}): 변경 {len(r.changes)}건")
        except Exception as e:                                       # noqa: BLE001
            errors.append(f"{name}({hit[0]}): 실행 오류 {str(e).splitlines()[0][:100]}")
    return Check("샌드박스 실행", not errors, errors + [f"실행해 볼 대상 없음: {', '.join(idle)}"] * bool(idle),
                 {"실행": len(ran), "오류": len(errors), "대상 없음": len(idle)})


def gate(data: dict, entries: dict[str, dict], qa: list[dict], review: Review | None, client: OntologyClient,
         active: Ontology | None, db_url: str | None = None) -> GateReport:
    """data: 검토를 반영해 조립한 정의, entries: 검토를 반영한 생성 Action, client: 같은 DB 의 엔진(샌드박스용)"""
    names = list(entries)
    checks: list[Check] = []
    # 1. 검증기
    errs = check(data, names)
    bad = [f"{n}: {e[0]}" for n, e in errs.items() if e]
    onto = None
    if not bad:
        onto = Ontology.model_validate(data)
        bad = [str(i) for i in validate(onto, TAXOS.parent).errors]
    checks.append(Check("검증기", not bad, bad, {"오류": len(bad)}))
    # 2. 검토 완료
    pending = [f"Action {n}: 전문가 결정 없음" for n, e in entries.items() if not e.get("decision")]
    gaps = [f"{q['id']} {q['question']}" for q in qa if not q.get("grounded") and not q.get("reviewed")]
    checks.append(Check("검토 완료", not pending and not gaps,
                        pending + [f"문서에 답이 없는 질문: {g}" for g in gaps],
                        {"결정 없는 Action": len(pending), "답 없는 질문": len(gaps)}))
    # 3. 변경 범위 (현재 활성 버전과 비교)
    if onto is not None and active is not None:
        changes = diff(active, onto)
        gone = set(active.actions) - set(onto.actions)
        outside = [str(c) for c in changes if not _owned(c.path, set(names) | gone)]
        breaking = [str(c) for c in changes if c.level == "breaking"]
        ok = not outside and (not breaking or bool(review and review.allow_breaking))
        checks.append(Check("변경 범위", ok, [f"검토 밖 변경: {x}" for x in outside] +
                            ([f"breaking 변경을 허용하지 않음: {x}" for x in breaking]
                             if breaking and not (review and review.allow_breaking) else []),
                            {"변경": len(changes), "breaking": len(breaking), "검토 밖": len(outside)}))
    else:
        checks.append(Check("변경 범위", onto is not None, ["정의를 읽을 수 없어 비교하지 못함"] * (onto is None)))
    if onto is None:
        return GateReport(checks)
    # 4. DB 대조
    from minionto.db import check_database, database_url
    rep = check_database(onto, database_url(onto, db_url))
    checks.append(Check("DB 대조", rep.ok, [str(i) for i in rep.errors]))
    # 5·6. 샌드박스와 Q/A 테스트 — 같은 연결, 항상 롤백
    with client.conn.transaction(force_rollback=True):
        client.actions.ensure_tables()
        c = OntologyClient(onto, base_dir=TAXOS.parent, today=client.today)
        c._conn = client.conn
        params = (lambda a: test_params(c, a))
        checks.append(sandbox(c, names, params, list(ACTOR_OF.values())))
        tests = build_tests(qa)
        res = run_tests(c, tests, task_map(qa, entries), params)
    s = summarize(res)
    checks.append(Check("Q/A 테스트", s["fail"] == 0, s["fails"],
                        {"테스트": s["tests"], "통과": s["pass"], "실패": s["fail"], "대상 없음": s["skip"]}))
    return GateReport(checks)


def reviewed_definition(base: dict, entries: dict[str, dict], version: str) -> dict:
    """검토를 반영한 정의. 36장 초안에서 비워 둔 구독은 대상 Action 이 같은 이름으로 있으면 되살린다"""
    data = assemble(base, entries)
    data["ontology"]["version"] = version
    subs = yaml.safe_load(TAXOS.read_text(encoding="utf-8")).get("subscriptions") or {}
    data["subscriptions"] = {k: v for k, v in subs.items() if v["action"] in data["actions"]}
    return data


__all__ = ["Review", "load_review", "apply_review", "patch", "renamed", "gate", "GateReport", "Check", "sandbox",
           "reviewed_definition"]
