"""37장. Q/A 기반 생성 — 실행과 평가 (36장 문서 직접 입력 방식과 같은 정답·같은 지표로 비교)

    uv run python 09-auto-ontology/37_qa.py                  # 1회
    uv run python 09-auto-ontology/37_qa.py --runs 3         # 3회

순서: 문서 → Q/A → 규칙·Action 후보 → MiniOnto 정의(36장 build) → 평가
평가: 36장과 같은 채점(규칙·Action·필드·검증 통과·판단·변경) + Q/A 테스트
      Q/A 테스트는 정답 정의에서도 돌린다 — 정답에서 실패하는 테스트는 테스트(곧 Q/A 의 답)가 틀린 것이다.
결과: results37/run_N.json, draft_N.yaml, qa_N.json. DB 변경은 없다 (판단만 하고 롤백).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from extract import GOLD, LLM, TAXOS, build, chunks, object_model  # noqa: E402
from qa import (build_tests, dump_tests, make_qa, number, organize, qa_stats, run_tests,  # noqa: E402
                summarize, task_map, test_params)

from minionto import OntologyClient  # noqa: E402
from minionto.metamodel import Ontology  # noqa: E402

ev = importlib.import_module("36_extract")                       # 36장 채점·비교 함수
TODAY, SCOPE = ev.TODAY, ev.SCOPE


def qa_tests(taxos: OntologyClient, draft_data: dict, qa: list[dict], actions: dict, named: list[tuple[str, str]]):
    """Q/A 테스트를 초안 정의와 정답 정의에서 각각 돌린다"""
    tests = build_tests(qa)
    to_draft = task_map(qa, actions)
    d2g = {d: g for g, d in named}
    to_gold = {i: d2g[a] for i, a in to_draft.items() if a in d2g}
    with taxos.conn.transaction(force_rollback=True):
        taxos.actions.ensure_tables()
        draft_c = OntologyClient(Ontology.model_validate(draft_data), base_dir=TAXOS.parent, today=TODAY)
        draft_c._conn = taxos.conn
        on_draft = run_tests(draft_c, tests, to_draft, lambda a: test_params(draft_c, a))
        on_gold = run_tests(taxos, tests, to_gold, lambda a: test_params(taxos, a))
    return on_draft, on_gold


def test_summary(on_draft, on_gold) -> dict:
    return {"draft": {x: v for x, v in summarize(on_draft).items() if x != "fails"},
            "gold": {x: v for x, v in summarize(on_gold).items() if x != "fails"},
            "caught": sum(1 for d, g in zip(on_draft, on_gold) if g.outcome == "pass" and d.outcome == "fail")}


def retest(out_dir: Path) -> None:
    """저장된 Q/A·초안으로 Q/A 테스트만 다시 돌린다 (LLM 호출 없음)"""
    from extract import assemble, base_definition
    gold = yaml.safe_load(GOLD.read_text(encoding="utf-8"))
    base = base_definition({a["name"] for a in gold["actions"]})
    taxos = OntologyClient.open(TAXOS, today=TODAY)
    for f in sorted(out_dir.glob("run_*.json")):
        k = f.stem.split("_")[1]
        data = json.loads(f.read_text(encoding="utf-8"))
        qa = json.loads((out_dir / f"qa_{k}.json").read_text(encoding="utf-8"))
        drafts = yaml.safe_load((out_dir / f"draft_{k}.yaml").read_text(encoding="utf-8"))
        acts = {n: {**d, "candidate": {"name": d.get("task"), "qa": d.get("qa") or []}} for n, d in drafts.items()}
        good = {n: a for n, a in acts.items() if not data["errors"].get(n)}
        on_draft, on_gold = qa_tests(taxos, assemble(base, good), qa, acts, [tuple(p) for p in data["pairs"]])
        data["summary"]["tests"] = test_summary(on_draft, on_gold)
        data["tests_draft"], data["tests_gold"] = dump_tests(on_draft), dump_tests(on_gold)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        show(data["summary"])
    taxos.close()


def run_once(k: int, llm: LLM, gold: dict, out_dir: Path) -> dict:
    t0 = time.time()
    taxos = OntologyClient.open(TAXOS, today=TODAY)
    model_text = object_model(taxos.onto, SCOPE)
    parts = chunks(True)
    qa = number([q for ch in parts for q in make_qa(llm, ch, model_text)])
    merged = organize(llm, qa, model_text)
    t1 = time.time()
    gold_names = {a["name"] for a in gold["actions"]}
    draft = build(llm, merged, model_text, gold_names)
    t2 = time.time()

    cands = list(draft.actions.items())
    verdict = ev.judge(llm, gold, merged["rules"], [e["candidate"] for _, e in cands])
    rules = ev.eval_rules(gold["rules"], merged["rules"], verdict.get("rules", []))
    gidx = {a["name"]: i for i, a in enumerate(gold["actions"])}
    named, used = [], set()
    for x in verdict.get("actions", []):
        j, g = x.get("i"), x.get("gold")
        if isinstance(j, int) and j < len(cands) and g in gidx and g not in used:
            used.add(g)
            named.append((g, cands[j][0]))
    per_action, fields, beh = ev.compare(taxos, gold, draft.data, dict(cands), draft.errors, named)
    on_draft, on_gold = qa_tests(taxos, draft.data, qa, draft.actions, named)
    found = {g for g, _ in named}
    summary = {
        "run": k, "chunks": len(parts), "qa": qa_stats(qa),
        "rules": {x: rules[x] for x in ("gold", "extracted", "mapped", "recall", "recall_same", "precision",
                                        "duplicates", "kind_agree", "recall_by_source")},
        "actions": {"gold": len(gold["actions"]), "candidates": len(cands), "found": len(found),
                    "missed": sorted(gold_names - found),
                    "extra": [e["candidate"].get("name") for n, e in cands if n not in {d for _, d in named}],
                    "field_agree": {f: f"{fields[f]}/{len(named)}" for f in ("target", "roles", "actor_types",
                                                                             "approval", "transition")}},
        "dsl": {"rounds": [{x: v for x, v in r.items() if x != "errors"} for r in draft.rounds],
                "valid_matched": f"{sum(1 for r in per_action if r['valid'])}/{len(named)}"},
        "behaviour": beh,
        "tests": test_summary(on_draft, on_gold),
        "seconds": {"qa": round(t1 - t0, 1), "dsl": round(t2 - t1, 1)},
    }
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"qa_{k}.json").write_text(json.dumps(qa, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"run_{k}.json").write_text(json.dumps(
        {"summary": summary, "pairs": named, "rules_detail": rules, "actions_detail": per_action,
         "errors": {n: e for n, e in draft.errors.items() if e}, "merged": merged,
         "tests_draft": dump_tests(on_draft), "tests_gold": dump_tests(on_gold)},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / f"draft_{k}.yaml").write_text(yaml.safe_dump(
        {n: {"definition": a["definition"], "transitions": a["transitions"],
             "task": a["candidate"].get("name"), "qa": a["candidate"].get("qa") or []}
         for n, a in draft.actions.items()}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    taxos.close()
    return summary


def show(s: dict) -> None:
    q, r, a, d, b, t = s["qa"], s["rules"], s["actions"], s["dsl"], s["behaviour"], s["tests"]
    print(f"\n== Q/A 기반 #{s['run']} (덩어리 {s['chunks']}개, Q/A {s['seconds']['qa']}초 · DSL {s['seconds']['dsl']}초)")
    print(f"  Q/A    {q['qa']}개 (근거 있음 {q['grounded']}, 문서에 없음 {q['gaps']}, 테스트로 쓸 수 있음 {q['with_expect']}), "
          f"업무 이름 {q['tasks']}종, 출처별 {q['by_source']}")
    print(f"  규칙   추출 {r['extracted']} · 정답 {r['gold']} → 재현율 {r['recall']:.2f} (같음만 {r['recall_same']:.2f}), "
          f"정밀도 {r['precision']:.2f}, 중복 {r['duplicates']}, 종류 일치 {r['kind_agree']}, 출처별 {r['recall_by_source']}")
    print(f"  Action 후보 {a['candidates']} · 정답 {a['gold']} · 찾음 {a['found']} (놓침 {a['missed']}, 정답 밖 {a['extra']})")
    print(f"         필드 일치 {a['field_agree']}")
    print(f"  DSL    검증 단계별 {d['rounds']} · 짝지은 Action 중 통과 {d['valid_matched']}")
    print(f"  행동   판단 일치 {b['same']}/{b['total']} ({b['rate']:.1%}) · 가능/불가 일치 {b['can_same']}/{b['total']} "
          f"({b['can_rate']:.1%}) · 변경 일치 {b['effects']}, 변경까지 모두 같은 Action {b['effects_actions']}")
    print(f"  테스트 초안 {t['draft']} / 정답 {t['gold']} · 초안의 결함을 잡은 테스트 {t['caught']}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", default="results37")
    ap.add_argument("--retest", action="store_true", help="저장된 결과로 Q/A 테스트만 다시 (LLM 없음)")
    args = ap.parse_args()
    if args.retest:
        retest(HERE / args.out)
        return 0
    from dotenv import load_dotenv
    load_dotenv(HERE.parent / ".env")
    gold = yaml.safe_load(GOLD.read_text(encoding="utf-8"))
    llm = LLM()
    for k in range(1, args.runs + 1):
        show(run_once(k, llm, gold, HERE / args.out))
    print(f"\nLLM 호출 {llm.usage['calls']}회, 입력 {llm.usage['prompt_tokens']:,} 토큰, "
          f"출력 {llm.usage['completion_tokens']:,} 토큰")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
