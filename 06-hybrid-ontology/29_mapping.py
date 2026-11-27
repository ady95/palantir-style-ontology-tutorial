"""29장. 의미 모델(OWL) ↔ 운영형 정의(MiniOnto) 매핑

    uv run python 06-hybrid-ontology/29_mapping.py

1. 매핑 검증        — 양쪽 이름, 빠진 것, 타입·도메인·카디널리티
2. 공리 확인        — OWL 의 분리·포함 공리를 운영 데이터에 질의
3. RDF 내보내기     — 운영 DB → RDF (엔진을 거쳐서)
4. PART 2 와 비교   — CSV 로 만든 07장 그래프와 트리플 단위로, 같은 SPARQL 의 답
5. 실행자별 내보내기 — 세무사 / 실무자 / AI 에이전트
6. OWL → 정의 초안  — 초안이 실제 정의와 얼마나 맞는가

DB 는 읽기만 한다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(ROOT / "01-traditional-ontology"))

from minionto import OntologyClient  # noqa: E402
from minionto.semantic import Mapping, axiom_checks, check, compare_draft, draft_from_owl, draft_yaml, export_rdf  # noqa: E402

MAPPING = HERE / "taxos_mapping.yaml"
AS_OF = "2027-01-18"
TAX = {"id": "A001", "role": "세무사", "type": "human"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def q2_count(g) -> int:
    from importlib import import_module
    q = import_module("02_sparql_queries").Q2_MISSING
    return len(list(g.query(q)))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    m = Mapping.load(MAPPING)

    section("1. 매핑 검증")
    rep = check(m)
    for k, (mapped, excluded, total) in rep.coverage.items():
        print(f"  {k:10s} 매핑 {mapped:3d} · 이유 있는 제외 {excluded:2d} · 전체 {total:3d}"
              f"{'' if mapped + excluded == total else '  ← 빠진 것 ' + str(total - mapped - excluded)}")
    for fd in rep.findings:
        print(f"  [{fd.level}] {fd.side}: {fd.message}")
    print(f"  오류 {len(rep.errors)}건")

    with OntologyClient.open(ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml",
                             today=AS_OF) as c:
        section("2. 공리를 운영 데이터로 확인")
        for a in axiom_checks(c, m):
            how = "구조상 성립 (같은 속성의 다른 값)" if a["structural"] else f"질의: {a['query']}"
            print(f"  {a['axiom']:55s} 위반 {a['violations']}건  ({how})")

        section("3. 운영 DB → RDF (세무사 권한)")
        t0 = time.perf_counter()
        g = export_rdf(c, m, actor=TAX)
        print(f"  트리플 {len(g):,}개 ({time.perf_counter() - t0:.1f}초)")

        section("4. PART 2(07장) 그래프와 비교")
        from importlib import import_module
        ref = import_module("01_build_graph").build()
        only_ours, only_ref = set(g) - set(ref), set(ref) - set(g)
        print(f"  07장 그래프 {len(ref):,} · 내보낸 그래프 {len(g):,} · 같은 트리플 {len(set(g) & set(ref)):,}")
        print(f"  내보낸 쪽에만 {len(only_ours)} · 07장 쪽에만 {len(only_ref)}")
        for t in sorted(only_ours)[:3] + sorted(only_ref)[:3]:
            print("   ", " ".join(m.short(x) if hasattr(x, "n3") and str(x).startswith("http") else repr(str(x))
                                  for x in t))
        from common import load_ontology
        print(f"  SPARQL Q2 (기한 임박 + 자료 미제출 영업 고객): 07장 {q2_count(load_ontology() + ref)}명 · "
              f"내보낸 그래프 {q2_count(load_ontology() + g)}명")

        section("5. 실행자별 내보내기 (권한 정책이 그대로 적용된다)")
        from rdflib import URIRef
        reg = URIRef("https://example.org/taxos#businessRegNo")
        cust = URIRef("https://example.org/taxos#Customer")
        for name, actor in (("세무사 A001", TAX), ("실무자 A003", STAFF), ("AI 에이전트", AI)):
            ga = export_rdf(c, m, actor=actor)
            n_cust = len(set(ga.subjects(URIRef("https://example.org/taxos#riskLevel"), None)))
            print(f"  {name:10s} 트리플 {len(ga):6,}개 · 고객 {n_cust:3d}명 · 사업자등록번호 "
                  f"{len(list(ga.triples((None, reg, None)))):3d}개")
        _ = cust

    section("6. OWL → 운영형 정의 초안")
    draft = draft_from_owl(m.owl)
    text = draft_yaml(draft)
    out = HERE / "output" / "taxos_draft.yaml"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"  객체 타입 {len(draft['object_types'])} · 링크 {len(draft['link_types'])} · 메모 {len(draft['notes'])}"
          f" → {out.relative_to(ROOT)}")
    for k, (hit, total) in compare_draft(draft, m).items():
        print(f"  {k:16s} {hit}/{total}")
    for n in draft["notes"]:
        print(f"  메모: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
