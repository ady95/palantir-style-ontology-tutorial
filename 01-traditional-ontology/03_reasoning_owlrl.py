"""08장(2). OWL-RL 추론 (owlrl, 순수 Python)

추론 전후로 같은 질의를 실행해, 명시하지 않은 사실이 공리로부터 채워지는 것을 확인합니다.
- rdfs:subClassOf      → 모든 고객이 tx:Customer
- 법인 ⊑ 일반과세자    → 법인 66곳이 tx:GeneralTaxpayer
- owl:inverseOf        → tx:manages(담당 고객) 생성
- 정의 클래스          → 기한후신고 이력 고객(tx:LateFiler), 연락두절 이력 고객 자동 분류

실행: uv run python 01-traditional-ontology/03_reasoning_owlrl.py
"""

from __future__ import annotations

import time

import owlrl
from rdflib import Graph

from common import load_all

PREFIX = "PREFIX tx: <https://example.org/taxos#>\n"

CHECKS = {
    "tx:Customer 인스턴스": "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a tx:Customer }",
    "tx:GeneralTaxpayer 인스턴스": "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a tx:GeneralTaxpayer }",
    "  └ 그중 법인": "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a tx:GeneralTaxpayer, tx:Corporation }",
    "tx:manages 관계": "SELECT (COUNT(*) AS ?n) WHERE { ?a tx:manages ?c }",
    "tx:RiskEvent 인스턴스": "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a tx:RiskEvent }",
    "tx:LateFiler (기한후신고 이력 고객)": "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a tx:LateFiler }",
    "tx:UnreachableCustomer (연락두절 이력)": "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a tx:UnreachableCustomer }",
}


def count(g: Graph, q: str) -> int:
    return int(next(iter(g.query(PREFIX + q)))[0])


def reason(g: Graph) -> Graph:
    """OWL 2 RL 규칙으로 그래프를 닫는다(원본 그래프에 추론 결과가 추가됨)."""
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(g)
    return g


def main() -> None:
    g = load_all()
    before = {k: count(g, q) for k, q in CHECKS.items()}
    n_before = len(g)

    t0 = time.perf_counter()
    reason(g)
    elapsed = time.perf_counter() - t0

    print(f"트리플 {n_before:,} → {len(g):,}  (+{len(g) - n_before:,}, 추론 {elapsed:.1f}초)\n")
    print(f"{'항목':40s} {'추론 전':>8s} {'추론 후':>8s}")
    for k, q in CHECKS.items():
        print(f"{k:40s} {before[k]:>8,d} {count(g, q):>8,d}")


if __name__ == "__main__":
    main()
