"""37장. 초안 생성부터 등록까지 — 검토 기록 적용, 관문, Registry 등록·활성화 (LLM 불필요)

    uv run python 09-auto-ontology/37_review.py                       # results37 의 1회차 초안 + review/review_1.yaml
    uv run python 09-auto-ontology/37_review.py --run 2 --review review/review_2.yaml

1. 초안      37_qa.py 가 만든 초안 정의(draft_N.yaml)와 Q/A(qa_N.json)를 읽는다
2. 관문      검토 전 — 여섯 가지 검사
3. 검토      전문가의 검토 기록(review/*.yaml)을 적용한다. 바뀐 내용은 모두 수정 기록으로 남는다
4. 관문      검토 후 — 통과해야 다음 단계로
5. Registry  세무사가 등록(draft) → AI·등록자 본인의 활성화는 거부 → 다른 세무사가 활성화 → 활성 버전으로 엔진 열기
6. 비교      검토 전·후 초안을 36장 방식으로 정답과 비교

Registry 와 업무 데이터의 변경은 모두 롤백한다 (실습 서버의 Registry 를 바꾸지 않는다).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from extract import GOLD, TAXOS, base_definition  # noqa: E402
from review import apply_review, gate, load_review, renamed, reviewed_definition  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.registry import REGISTRY_SCHEMA, Package, Registry, RegistryError  # noqa: E402

ev = importlib.import_module("36_extract")
TODAY = ev.TODAY
VERSION = "0.9.0"
A005 = {"id": "A005", "role": "세무사", "type": "human"}
BUILDER = {"id": "onto-builder", "role": "AI비서", "type": "agent"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def measure(taxos: OntologyClient, gold: dict, data: dict, entries: dict, pairs: list[tuple[str, str]]) -> dict:
    errors = {n: [] for n in entries}
    _, fields, beh = ev.compare(taxos, gold, data, entries, errors, [p for p in pairs if p[1] in entries])
    n = len(pairs)
    return {"fields": {f: f"{fields[f]}/{n}" for f in ("target", "roles", "actor_types", "approval", "transition")},
            "judge": f"{beh['same']}/{beh['total']} ({beh['rate']:.1%})", "can": f"{beh['can_rate']:.1%}",
            "effects": beh["effects"], "effects_actions": beh["effects_actions"]}


def registry_flow(taxos: OntologyClient, data: dict, review, report) -> None:
    reg = Registry()
    sources = Package.from_file(TAXOS).function_sources
    with reg.conn.transaction(force_rollback=True):
        reg.conn.execute(f"DELETE FROM {REGISTRY_SCHEMA}.ontology_versions WHERE name = 'taxos'")   # 롤백됨
        base = reg.register(TAXOS, actor=review.reviewer)
        reg.activate("taxos", base.version.version, actor=A005)
        print(f"  기준: 손으로 쓴 정의 {reg.get('taxos')} (등록 A001 · 활성화 A005)")
        pkg = Package.from_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), sources)
        r = reg.register(pkg, actor=review.reviewer, notes=f"37장 Q/A 기반 생성 · 검토 {review.reviewer['id']} · "
                                                           + report.notes())
        lv = Counter(c.level for c in r.changes)
        print(f"  등록: {r.version} (등록자 {review.reviewer['id']}) — v{r.base} 대비 변경 {len(r.changes)}건 {dict(lv)}, "
              f"권장 버전 {r.suggested_version}")
        for c in r.changes[:8]:
            print(f"    {c}")
        if len(r.changes) > 8:
            print(f"    … 외 {len(r.changes) - 8}건")
        for who, label in ((BUILDER, "AI(onto-builder)"), (review.reviewer, f"등록자 본인({review.reviewer['id']})")):
            try:
                reg.activate("taxos", VERSION, actor=who, allow_breaking=bool(review.allow_breaking))
                print(f"  활성화 {label}: 통과 (예상과 다름)")
            except RegistryError as e:
                print(f"  활성화 {label}: 거부 — {str(e).splitlines()[0]}")
        try:
            reg.activate("taxos", VERSION, actor=A005, allow_breaking=bool(review.allow_breaking))
            print(f"  활성화 A005: 통과 → 활성 버전 {reg.get('taxos')}")
        except RegistryError as e:
            print(f"  활성화 A005: 거부 — {e}")
            return
        for h in reg.history("taxos"):
            print(f"    {h['version']:6} {h['status']:8} 등록 {h['registered_by']:5} 활성화 {h['activated_by'] or '-'}")
        client = reg.get("taxos").client(today=TODAY)
        print(f"  활성 버전으로 연 엔진의 Action {len(client.onto.actions)}개 — 정의 v{client.onto.ontology.version}")
        client.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--results", default="results37")
    ap.add_argument("--review", default="review/review_1.yaml")
    args = ap.parse_args()
    out = HERE / args.results
    drafts = yaml.safe_load((out / f"draft_{args.run}.yaml").read_text(encoding="utf-8"))
    qa = json.loads((out / f"qa_{args.run}.json").read_text(encoding="utf-8"))
    run = json.loads((out / f"run_{args.run}.json").read_text(encoding="utf-8"))
    gold = yaml.safe_load(GOLD.read_text(encoding="utf-8"))
    gold_names = {a["name"] for a in gold["actions"]}
    base = base_definition(gold_names)
    entries = {n: {"definition": d["definition"], "transitions": d["transitions"],
                   "candidate": {"name": d.get("task"), "qa": d.get("qa") or []}}
               for n, d in drafts.items() if not run["errors"].get(n)}
    pairs = [tuple(p) for p in run["pairs"]]
    taxos = OntologyClient.open(TAXOS, today=TODAY)

    section(f"1. 초안 — {args.results}/draft_{args.run}.yaml")
    gaps = [q for q in qa if not q.get("grounded")]
    print(f"  생성된 Action {len(drafts)}개 (검증 통과 {len(entries)}), Q/A {len(qa)}개 (문서에 답이 없는 질문 {len(gaps)}개)")
    for n, e in entries.items():
        print(f"    {n:28} {e['candidate']['name']}")

    section("2. 관문 — 검토 전")
    data0 = reviewed_definition(base, {n: {**e, "decision": None} for n, e in entries.items()}, VERSION)
    gate(data0, {n: {**e, "decision": None} for n, e in entries.items()}, qa, None, taxos, taxos.onto).show()

    section(f"3. 전문가 검토 — {args.review}")
    review = load_review(HERE / args.review)
    reviewed, qa2, log = apply_review(entries, qa, review)
    kinds = Counter(x["change"] for x in log)
    print(f"  검토자 {review.reviewer['id']} · 수정 기록 {len(log)}건 {dict(kinds)}")
    for x in log:
        if x["target"].startswith("action:") and x["change"] not in ("approve",):
            what = x.get("path") or ""
            print(f"    {x['target']:34} {x['change']:6} {what:34} — {x.get('reason') or ''}")
    print(f"  Q/A 검토 {sum(1 for x in log if x['target'].startswith('qa:'))}건 "
          f"(문서에 없던 질문에 답 {sum(1 for x in log if x['target'].startswith('qa:') and x['change'] == 'answer')}, "
          f"답 고침 {sum(1 for x in log if x['target'].startswith('qa:') and x['change'] == 'fix')}, "
          f"범위 밖 {sum(1 for x in log if x['target'].startswith('qa:') and x['change'] == 'out_of_scope')})")
    (out / f"review_log_{args.run}.json").write_text(json.dumps(log, ensure_ascii=False, indent=1, default=str),
                                                     encoding="utf-8")

    section("4. 관문 — 검토 후")
    data = reviewed_definition(base, reviewed, VERSION)
    report = gate(data, reviewed, qa2, review, taxos, taxos.onto)
    report.show()

    section("5. Registry 등록과 활성화 (롤백)")
    if report.ok:
        registry_flow(taxos, data, review, report)
    else:
        print("  관문을 통과하지 못해 등록하지 않습니다")

    section("6. 정답과 비교 — 검토 전·후 (36장 방식)")
    rn = renamed(review)
    before = measure(taxos, gold, data0, entries, pairs)
    after = measure(taxos, gold, data, reviewed, [(g, rn.get(d, d)) for g, d in pairs])
    for k in before:
        print(f"  {k:16} 검토 전 {before[k]}\n  {'':16} 검토 후 {after[k]}")
    taxos.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
