"""17장. 같은 질문을 SPARQL(PART 2)과 Object Engine으로 풀어 결과를 대조한다

- SPARQL: 01-traditional-ontology 의 RDF 그래프(CSV → Turtle)에 Q2·Q3·Q4 실행
- 엔진:   운영 DB(PostgreSQL) 위의 온톨로지 이름으로 같은 질문
결과 ID 집합이 같으면, 질문의 '의미'가 저장 방식(RDF ↔ RDB)과 무관하게 옮겨졌다는 뜻이다.

실행: cd 03-ontology-engine && uv run python examples/17_compare_sparql.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                              # minionto
sys.path.insert(0, str(HERE.parents[2] / "01-traditional-ontology"))  # PART 2 common

from common import AS_OF, CURRENT_DUE, load_all  # noqa: E402
from minionto import OntologyClient  # noqa: E402

sparql_mod = __import__("02_sparql_queries")
ONTOLOGY = HERE.parents[1] / "ontologies" / "taxos" / "ontology.yaml"


def sparql_ids(g, query: str) -> set[str]:
    """SPARQL 결과의 첫 열(ID)을 집합으로"""
    return {str(row[0]) for row in g.query(query)}


def engine_ids(onto: OntologyClient) -> dict[str, set[str]]:
    current_open = {"all": [
        {"field": "status", "op": "eq", "value": "자료수집중"},
        {"field": "due_date", "op": "gte", "value": "$today"},
        {"field": "due_date", "op": "lte", "value": CURRENT_DUE},
        {"some": {"link": "submissions",
                  "where": {"field": "status", "op": "in", "value": ["미요청", "요청됨"]}}},
    ]}
    q2 = (onto.objects("Customer")
          .where(business_status="영업")
          .where({"some": {"link": "tax_returns", "where": current_open}}))
    q3 = (onto.objects("TaxReturn")
          .where(due_date=CURRENT_DUE)
          .where({"none": {"link": "submissions"}}))
    q4 = onto.objects("Customer").count_by("accountant.accountant_id", "accountant.max_workload")
    return {
        "Q2": {c.pk for c in q2},
        "Q3": {r.customer_id for r in q3},          # SPARQL Q3 는 고객 ID를 돌려준다
        "Q4": {r["accountant.accountant_id"] for r in q4 if r["count"] > r["accountant.max_workload"]},
    }


def compare() -> dict[str, tuple[set[str], set[str]]]:
    g = load_all()
    sparql = {
        "Q2": sparql_ids(g, sparql_mod.Q2_MISSING),
        "Q3": sparql_ids(g, sparql_mod.Q3_UNREGISTERED),
        "Q4": sparql_ids(g, sparql_mod.Q4_WORKLOAD),
    }
    with OntologyClient.open(ONTOLOGY, today=AS_OF) as onto:
        engine = engine_ids(onto)
    return {k: (sparql[k], engine[k]) for k in sparql}


def main() -> int:
    titles = {"Q2": "기한 임박 + 자료 미제출 고객", "Q3": "자료 현황 미등록 신고의 고객",
              "Q4": "업무량 한도 초과 담당자"}
    all_same = True
    for key, (s, e) in compare().items():
        same = s == e
        all_same &= same
        print(f"{key} {titles[key]:20s} SPARQL {len(s):3d}건 · 엔진 {len(e):3d}건 · "
              f"{'일치' if same else '불일치'}")
        if not same:
            print(f"   SPARQL에만: {sorted(s - e)}")
            print(f"   엔진에만  : {sorted(e - s)}")
        elif len(s) <= 6:
            print(f"   {sorted(s)}")
    return 0 if all_same else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
