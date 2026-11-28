"""36장. 업무 매뉴얼·상담 로그에서 규칙과 Action 추출 — 실행과 평가

    uv run python 09-auto-ontology/36_extract.py                  # 매뉴얼 + 상담 로그, 1회
    uv run python 09-auto-ontology/36_extract.py --runs 3         # 3회 반복 (LLM 결과는 매번 다를 수 있다)
    uv run python 09-auto-ontology/36_extract.py --no-logs        # 매뉴얼만 (상담 로그의 기여를 본다)

평가 (정답: gold/gold.yaml, 세무 정의 taxos v0.8.0)
  규칙    LLM 채점자가 추출 규칙마다 같은 정답 규칙을 고른다 → 재현율·정밀도·중복, 종류 일치
  Action  LLM 채점자가 추출 Action 마다 같은 정답 Action 을 고른다 → 찾은 비율, 대상·역할·실행자 유형·승인·상태 전이 일치
  DSL     변환한 정의가 검증기를 통과한 비율 (수리 전·후)
  행동    통과한 Action 과 정답 Action 의 판단(check)을 같은 대상·실행자로 비교 → 판단 일치율
결과는 results/ 에 저장한다. DB 변경은 없다 (판단만 하고 롤백).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from extract import GOLD, LLM, TAXOS, Embedder, build, chunks, extract, merge, object_model  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.metamodel import Ontology  # noqa: E402

TODAY = "2027-01-18"
ACTORS = [{"id": "A001", "role": "세무사", "type": "human"}, {"id": "A002", "role": "실무자", "type": "human"},
          {"id": "agent-01", "role": "AI비서", "type": "agent"}]
SCOPE = {"Accountant", "Customer", "TaxReturn", "DocumentType", "DocumentSubmission", "Task", "Inquiry"}


JUDGE_SYSTEM = """너는 채점자다. 정답 목록과 추출 목록을 비교해 같은 내용을 짝짓는다. 결과는 JSON 하나로만 답한다.
- 규칙: 추출 규칙마다 같은 규칙을 말하는 정답 id 를 하나 고른다. 표현이 달라도 조건·대상·주체가 같으면 same,
  한쪽이 더 넓거나 일부만 같으면 partial, 정답 목록에 해당하는 것이 없으면 gold 를 null 로 한다.
  같은 정답에 추출 규칙 여러 개가 짝지어져도 된다 (중복 추출).
- Action: 추출 Action 마다 같은 업무인 정답 Action 이름을 고르고, 없으면 null. 이름이 달라도 하는 일이 같으면 같은 업무다.
형식: {"rules": [{"i": 번호, "gold": id 또는 null, "match": "same"|"partial"|null}],
       "actions": [{"i": 번호, "gold": 이름 또는 null}]}"""


def judge(llm: LLM, gold: dict, rules: list[dict], cands: list[dict]) -> dict:
    """LLM 채점자로 추출 결과와 정답을 짝짓는다 (36-3 에서 사람이 채점한 결과와 맞춰 본다)"""
    nl = chr(10)
    user = (f"# 정답 규칙{nl}" + nl.join(f"{g['id']}: {g['rule']}" for g in gold["rules"]) +
            f"{nl}{nl}# 정답 Action{nl}" + nl.join(f"{a['name']}: {a['display']} (대상 {a['target']})"
                                                for a in gold["actions"]) +
            f"{nl}{nl}# 추출 규칙{nl}" + nl.join(f"{i}: {r['statement']}" for i, r in enumerate(rules)) +
            f"{nl}{nl}# 추출 Action{nl}" + nl.join(
                f"{i}: {c.get('name')} — {c.get('description') or ''} (대상 {c.get('target')})"
                for i, c in enumerate(cands)))
    return llm.json(JUDGE_SYSTEM, user)


def eval_rules(gold: list[dict], got: list[dict], verdict: list[dict]) -> dict:
    ids = {g["id"]: g for g in gold}
    v = {x.get("i"): x for x in verdict if isinstance(x.get("i"), int) and x.get("gold") in ids}
    same = {x["gold"] for x in v.values() if x.get("match") == "same"}
    any_ = {x["gold"] for x in v.values()}
    by_src = Counter(g["source"] for g in gold)
    hit_src = Counter(ids[g]["source"] for g in any_)
    kind_ok = sum(1 for i, x in v.items() if i < len(got) and got[i].get("kind") == ids[x["gold"]]["kind"])
    return {
        "gold": len(gold), "extracted": len(got), "mapped": len(v),
        "recall": round(len(any_) / len(gold), 3), "recall_same": round(len(same) / len(gold), 3),
        "precision": round(len(v) / len(got), 3) if got else 0,
        "duplicates": len(v) - len(any_), "kind_agree": f"{kind_ok}/{len(v)}",
        "recall_by_source": {s: f"{hit_src[s]}/{n}" for s, n in by_src.items()},
        "missed": [g["id"] + " " + g["rule"] for g in gold if g["id"] not in any_],
        "unmatched": [r["statement"] for i, r in enumerate(got) if i not in v],
        "pairs": [(x["gold"], got[i]["statement"], x.get("match")) for i, x in sorted(v.items()) if i < len(got)],
    }


def gold_spec(onto: Ontology, name: str) -> dict:
    a = onto.actions[name]
    t = None
    sm = onto.object_types[a.target].state_machine
    if sm and name in sm.transitions:
        t = {"from": sorted(sm.transitions[name].from_), "to": sm.transitions[name].to}
    return {"target": a.target, "roles": sorted(a.permission.roles), "actor_types": sorted(a.permission.actor_types),
            "approval": a.approval is not None, "transition": t, "params": sorted(a.parameters)}


def draft_spec(entry: dict) -> dict:
    from extract import transitions_of
    d = entry["definition"] if isinstance(entry["definition"], dict) else {}
    perm = d.get("permission") or {}
    t = next((x for x in transitions_of(entry) if x.get("object_type") == d.get("target")), None)
    if t:
        frm = t.get("from")
        t = {"from": sorted(frm if isinstance(frm, list) else [frm]), "to": t.get("to")}
    return {"target": d.get("target"), "roles": sorted(perm.get("roles") or []),
            "actor_types": sorted(perm.get("actor_types") or ["agent", "human"]),
            "approval": bool(d.get("approval")), "transition": t, "params": sorted(d.get("parameters") or {})}


def sample_params(onto: Ontology, c: OntologyClient, action: str, ref: dict | None = None) -> dict:
    """판단에 넣을 입력. ref(정답 쪽 입력)가 있으면 같은 이름은 같은 값, 객체를 가리키는 입력은 정답의 객체 키를 쓴다"""
    out = {}
    ref = ref or {}
    ref_objs = [v for k, v in ref.items() if k.startswith("__obj__")]
    for n, p in onto.actions[action].parameters.items():
        if n in ref:
            out[n] = ref[n]
        elif p.type == "object":
            o = c.objects(p.object_type).order_by(onto.object_types[p.object_type].source.primary_key).first()
            out[n] = ref_objs[0] if ref_objs else (o.pk if o else None)
        elif p.type == "enum":
            out[n] = p.values[-1]
        elif p.type == "string" and n.endswith("_id") and ref_objs:   # 객체 대신 키 문자열로 받는 입력
            out[n] = ref_objs[0]
        else:
            out[n] = {"integer": 1, "decimal": 1, "float": 1.0, "boolean": True, "date": TODAY}.get(p.type, "테스트")
    return out


def behaviour(gold_c: OntologyClient, draft_c: OntologyClient, gname: str, dname: str, n: int = 30) -> dict:
    """같은 대상·실행자·입력으로 정답 Action 과 초안 Action 의 판단(check) 결과를 비교. 대상 객체가 다르면 비교하지 않는다"""
    go, do = gold_c.onto, draft_c.onto
    target = go.actions[gname].target
    if do.actions[dname].target != target:
        return {"comparable": False, "same": 0, "total": 0, "rate": None,
                "diff": {f"대상이 다름: 정답 {target} / 초안 {do.actions[dname].target}": 1}}
    pk = go.object_types[target].source.primary_key
    objs = gold_c.objects(target).order_by(pk).all()
    step = max(len(objs) // n, 1)
    keys = [o.pk for o in objs[::step][:n]]
    gp = sample_params(go, gold_c, gname)
    ref = dict(gp)
    for k, p in go.actions[gname].parameters.items():
        if p.type == "object":
            ref[f"__obj__{k}"] = gp[k]
    dp = sample_params(do, draft_c, dname, ref)
    same, can, diff = 0, 0, Counter()
    ok = {"ready", "approval_required"}                   # 할 수 있다(승인 대기 포함) / 그 밖은 못 한다
    for k in keys:
        for a in ACTORS:
            g = gold_c.actions.check(gname, k, actor=a, params=gp).status
            d = draft_c.actions.check(dname, k, actor=a, params=dp).status
            same += g == d
            can += (g in ok) == (d in ok)
            if g != d:
                diff[f"{a['role']}: 정답 {g} / 초안 {d}"] += 1
    total = len(keys) * len(ACTORS)
    eff = effects(gold_c, draft_c, gname, dname, keys, gp, dp)
    return {"comparable": True, "same": same, "can_same": can, "total": total, "rate": round(same / total, 3),
            "diff": dict(diff.most_common(4)), "effects": eff}


def _norm(changes: list[dict]) -> set:
    """변경 기록을 비교할 수 있게: 수정은 (객체, 키, 속성, 값), 생성은 (객체, 속성, 값)들. 시각($now)·새 키는 뺀다"""
    from datetime import datetime
    out = set()
    for c in changes:
        vals = {k: v for k, v in (c.get("after") or {}).items() if not isinstance(v, datetime)}
        if c["op"] == "update":
            out |= {("update", c["object_type"], str(c["pk"]), k, str(v)) for k, v in vals.items()}
        else:
            pk = None
            out.add(("create", c["object_type"], tuple(sorted((k, str(v)) for k, v in vals.items()
                                                              if k != pk and str(v) != str(c.get("pk"))))))
    return out


def effects(gold_c: OntologyClient, draft_c: OntologyClient, gname: str, dname: str, keys: list, gp: dict,
            dp: dict, n: int = 5) -> dict:
    """정답이 실행할 수 있는 대상 n개에서 두 Action 을 실제로 실행(롤백)해 바뀐 내용을 비교한다"""
    tried, same, examples = 0, 0, []
    for k in keys:
        actor = next((a for a in ACTORS if gold_c.actions.check(gname, k, actor=a, params=gp).status == "ready"), None)
        if actor is None:
            continue
        with gold_c.conn.transaction(force_rollback=True):
            g = gold_c.actions.apply(gname, k, actor=actor, params=gp)
        with draft_c.conn.transaction(force_rollback=True):
            d = draft_c.actions.apply(dname, k, actor=actor, params=dp)
        tried += 1
        gs, ds = _norm(g.changes), _norm(d.changes) if d.status == "completed" else set()
        if g.status == d.status and gs == ds:
            same += 1
        elif len(examples) < 2:
            examples.append({"target": str(k), "actor": actor["role"], "draft_status": d.status,
                             "only_gold": sorted(map(str, gs - ds))[:3], "only_draft": sorted(map(str, ds - gs))[:3]})
        if tried >= n:
            break
    return {"tried": tried, "same": same, "examples": examples}


def compare(taxos: OntologyClient, gold: dict, draft_data: dict, entries: dict[str, dict], errors: dict,
            pairs: list[tuple[str, str]]) -> tuple[list[dict], Counter, dict]:
    """짝지은 Action 마다 필드 일치와 판단 일치를 잰다"""
    fields, per_action = Counter(), []
    with taxos.conn.transaction(force_rollback=True):
        taxos.actions.ensure_tables()
        draft_c = OntologyClient(Ontology.model_validate(draft_data), base_dir=TAXOS.parent, today=TODAY)
        draft_c._conn = taxos.conn                                  # 같은 연결·같은 롤백 범위
        for gname, dname in pairs:
            entry = entries[dname]
            gs, ds = gold_spec(taxos.onto, gname), draft_spec(entry)
            agree = {f: gs[f] == ds[f] for f in ("target", "roles", "actor_types", "approval", "transition")}
            fields.update(f for f, ok in agree.items() if ok)
            row = {"gold": gname, "draft": dname, "name": (entry.get("candidate") or {}).get("name"),
                   "valid": not errors.get(dname), "agree": agree, "gold_spec": gs, "draft_spec": ds}
            if row["valid"]:
                row["behaviour"] = behaviour(taxos, draft_c, gname, dname)
            per_action.append(row)
    comp = [r for r in per_action if r.get("behaviour", {}).get("comparable")]
    same, total = sum(r["behaviour"]["same"] for r in comp), sum(r["behaviour"]["total"] for r in comp)
    can = sum(r["behaviour"]["can_same"] for r in comp)
    eff_t = sum(r["behaviour"]["effects"]["tried"] for r in comp)
    eff_s = sum(r["behaviour"]["effects"]["same"] for r in comp)
    beh = {"same": same, "total": total, "rate": round(same / total, 3) if total else 0,
           "can_same": can, "can_rate": round(can / total, 3) if total else 0,
           "effects": f"{eff_s}/{eff_t}",
           "effects_actions": sum(1 for r in comp if r["behaviour"]["effects"]["tried"]
                                  and r["behaviour"]["effects"]["same"] == r["behaviour"]["effects"]["tried"]),
           "compared": f"{len(comp)}/{len(per_action)}",
           "perfect": sum(1 for r in comp if r["behaviour"]["rate"] == 1.0)}
    return per_action, fields, beh


def run_once(k: int, include_logs: bool, llm: LLM, emb: Embedder, gold: dict, out_dir: Path) -> dict:
    t0 = time.time()
    taxos = OntologyClient.open(TAXOS, today=TODAY)
    model_text = object_model(taxos.onto, SCOPE)
    parts = chunks(include_logs)
    results = [extract(llm, ch, model_text) for ch in parts]
    merged = merge(emb, results)
    t1 = time.time()
    gold_names = {a["name"] for a in gold["actions"]}
    draft = build(llm, merged, model_text, gold_names)
    t2 = time.time()

    cands = list(draft.actions.items())
    verdict = judge(llm, gold, merged["rules"], [e["candidate"] for _, e in cands])
    rules = eval_rules(gold["rules"], merged["rules"], verdict.get("rules", []))
    gidx = {a["name"]: i for i, a in enumerate(gold["actions"])}
    pairs, used = [], set()
    for x in verdict.get("actions", []):
        j, g = x.get("i"), x.get("gold")
        if isinstance(j, int) and j < len(cands) and g in gidx and g not in used:
            used.add(g)
            pairs.append((gidx[g], j, None))
    named = [(gold["actions"][i]["name"], cands[j][0]) for i, j, _ in pairs]
    per_action, fields, beh = compare(taxos, gold, draft.data, dict(cands), draft.errors, named)
    found = {g for g, _ in named}
    valid_rows = [r for r in per_action if r["valid"]]
    summary = {
        "run": k, "include_logs": include_logs, "chunks": len(parts),
        "rules": {x: rules[x] for x in ("gold", "extracted", "mapped", "recall", "recall_same", "precision",
                                        "duplicates", "kind_agree", "recall_by_source")},
        "actions": {"gold": len(gold["actions"]), "candidates": len(cands), "found": len(found),
                    "missed": sorted(gold_names - found),
                    "extra": [cands[j][1]["candidate"].get("name") for j in range(len(cands))
                              if j not in {j for _, j, _ in pairs}],
                    "field_agree": {f: f"{fields[f]}/{len(pairs)}" for f in ("target", "roles", "actor_types",
                                                                             "approval", "transition")}},
        "dsl": {"rounds": [{k: v for k, v in r.items() if k != "errors"} for r in draft.rounds],
                "round_errors": [r.get("errors", {}) for r in draft.rounds], "valid_matched": f"{len(valid_rows)}/{len(pairs)}"},
        "behaviour": beh,
        "seconds": {"extract": round(t1 - t0, 1), "dsl": round(t2 - t1, 1)},
    }
    tag = f"{'all' if include_logs else 'manual'}_{k}"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"run_{tag}.json").write_text(json.dumps(
        {"summary": summary, "rules_detail": rules, "actions_detail": per_action,
         "errors": {n: e for n, e in draft.errors.items() if e}, "merged": merged},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / f"draft_{tag}.yaml").write_text(yaml.safe_dump(
        {n: {"definition": a["definition"], "transitions": a["transitions"]} for n, a in draft.actions.items()},
        allow_unicode=True, sort_keys=False), encoding="utf-8")
    taxos.close()
    return summary


def show(s: dict) -> None:
    r, a, d, b = s["rules"], s["actions"], s["dsl"], s["behaviour"]
    print(f"\n== {'매뉴얼+상담 로그' if s['include_logs'] else '매뉴얼만'} #{s['run']} (덩어리 {s['chunks']}개, "
          f"추출 {s['seconds']['extract']}초 · DSL {s['seconds']['dsl']}초)")
    print(f"  규칙   추출 {r['extracted']} · 정답 {r['gold']} → 재현율 {r['recall']:.2f} (같음만 {r['recall_same']:.2f}), "
          f"정밀도 {r['precision']:.2f}, 중복 {r['duplicates']}, 종류 일치 {r['kind_agree']}, 출처별 {r['recall_by_source']}")
    print(f"  Action 후보 {a['candidates']} · 정답 {a['gold']} · 찾음 {a['found']} (놓침 {a['missed']}, 정답 밖 {a['extra']})")
    print(f"         필드 일치 {a['field_agree']}")
    print(f"  DSL    검증 단계별 {d['rounds']} · 짝지은 Action 중 통과 {d['valid_matched']}")
    print(f"  행동   판단 일치 {b['same']}/{b['total']} ({b['rate']:.1%}) · 가능/불가 일치 {b.get('can_same')}/{b['total']} "
          f"({b.get('can_rate', 0):.1%}) · 비교한 Action {b.get('compared')} (대상이 같은 것만), 완전 일치 {b.get('perfect')}")
    print(f"         변경 일치 {b.get('effects')} (정답이 실행 가능한 대상에서 실제 실행·롤백), 변경까지 모두 같은 Action "
          f"{b.get('effects_actions')}")


def rescore(gold: dict, out_dir: Path) -> None:
    """저장된 초안으로 필드·판단 비교만 다시 한다 (LLM 호출 없음 — 추출·변환·채점 결과는 그대로)"""
    from extract import assemble, base_definition
    gold_names = {a["name"] for a in gold["actions"]}
    taxos = OntologyClient.open(TAXOS, today=TODAY)
    for f in sorted(out_dir.glob("run_*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        drafts = yaml.safe_load((out_dir / f.name.replace("run_", "draft_").replace(".json", ".yaml"))
                                .read_text(encoding="utf-8"))
        errors = data.get("errors", {})
        names = {r["draft"]: r["name"] for r in data["actions_detail"]}
        entries = {n: {**a, "candidate": {"name": names.get(n)}} for n, a in drafts.items()}
        good = {n: a for n, a in drafts.items() if not errors.get(n)}
        draft_data = assemble(base_definition(gold_names), good)
        pairs = [(r["gold"], r["draft"]) for r in data["actions_detail"]]
        per_action, fields, beh = compare(taxos, gold, draft_data, entries, errors, pairs)
        s = data["summary"]
        s["behaviour"] = beh
        s["actions"]["field_agree"] = {k: f"{fields[k]}/{len(pairs)}" for k in
                                       ("target", "roles", "actor_types", "approval", "transition")}
        data["actions_detail"] = per_action
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        show(s)
    taxos.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-logs", action="store_true")
    ap.add_argument("--rescore", action="store_true", help="저장된 결과의 판단 비교만 다시 (LLM 없음)")
    ap.add_argument("--out", default="results", help="결과 폴더 (09-auto-ontology 아래)")
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(HERE.parent / ".env")
    gold = yaml.safe_load(GOLD.read_text(encoding="utf-8"))
    if args.rescore:
        rescore(gold, HERE / args.out)
        return 0
    llm, emb = LLM(), Embedder()
    out = []
    for k in range(1, args.runs + 1):
        s = run_once(k, not args.no_logs, llm, emb, gold, HERE / args.out)
        show(s)
        out.append(s)
    print(f"\nLLM 호출 {llm.usage['calls']}회, 입력 {llm.usage['prompt_tokens']:,} 토큰, "
          f"출력 {llm.usage['completion_tokens']:,} 토큰")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
