"""08장(1). SPARQL 질의

책의 공통 업무를 SPARQL로 표현합니다.
  "신고 기한이 임박한 고객 중 필수 자료를 제출하지 않은 고객을 찾는다"

실행: uv run python 01-traditional-ontology/02_sparql_queries.py
"""

from __future__ import annotations

from common import AS_OF, CURRENT_DUE, load_all, print_rows

PREFIX = """
PREFIX tx:   <https://example.org/taxos#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
"""

# Q1. 기본 조회: 사업자 유형별 고객 수 (명시된 타입만 — 추론 전)
Q1_TYPES = PREFIX + """
SELECT ?type (COUNT(?c) AS ?n) WHERE {
  VALUES ?type { tx:Customer tx:Corporation tx:IndividualBusiness tx:GeneralTaxpayer tx:SimplifiedTaxpayer }
  ?c a ?type .
} GROUP BY ?type ORDER BY ?type
"""

# Q2. 공통 업무: 기한 7일 이내 + 미제출(미요청·요청됨) 자료가 남은 영업 고객
Q2_MISSING = PREFIX + f"""
SELECT ?cid ?name ?risk ?staffName (COUNT(?s) AS ?missing) (GROUP_CONCAT(?doc; separator=", ") AS ?docs)
WHERE {{
  ?c tx:hasTaxReturn ?r ; tx:id ?cid ; tx:name ?name ; tx:riskLevel ?risk ;
     tx:businessStatus "영업" ; tx:managedBy/tx:name ?staffName .
  ?r tx:dueDate ?due ; tx:returnStatus "자료수집중" ; tx:hasSubmission ?s .
  ?s tx:submissionStatus ?st ; tx:documentType/tx:name ?doc .
  FILTER (?st IN ("미요청", "요청됨"))
  FILTER (?due >= "{AS_OF}"^^xsd:date && ?due <= "{CURRENT_DUE}"^^xsd:date)
}}
GROUP BY ?cid ?name ?risk ?staffName
ORDER BY DESC(?missing) ?cid
"""

# Q3. Open World 문제: 자료 제출 기록이 "하나도 없는" 현재 신고
#     FILTER NOT EXISTS 는 '그래프에 없으면 없다'고 보는 닫힌 세계식 질의다.
Q3_UNREGISTERED = PREFIX + f"""
SELECT ?cid ?name ?status WHERE {{
  ?c tx:hasTaxReturn ?r ; tx:id ?cid ; tx:name ?name .
  ?r tx:dueDate "{CURRENT_DUE}"^^xsd:date ; tx:returnStatus ?status .
  FILTER NOT EXISTS {{ ?r tx:hasSubmission ?any }}
}} ORDER BY ?cid
"""

# Q4. 관계 탐색: 담당 고객 수가 한도를 넘은 담당자 (property path + 집계)
Q4_WORKLOAD = PREFIX + """
SELECT ?aid ?name ?max (COUNT(?c) AS ?customers) WHERE {
  ?c tx:managedBy ?a .
  ?a tx:id ?aid ; tx:name ?name ; tx:maxWorkload ?max .
} GROUP BY ?aid ?name ?max
HAVING (COUNT(?c) > ?max)
ORDER BY DESC(?customers)
"""

QUERIES = {
    "Q1. 유형별 고객 수 (추론 전)": Q1_TYPES,
    "Q2. 기한 임박 + 자료 미제출 고객": Q2_MISSING,
    "Q3. 자료 현황 미등록 신고 (Open World 문제)": Q3_UNREGISTERED,
    "Q4. 업무량 한도 초과 담당자": Q4_WORKLOAD,
}


def main() -> None:
    g = load_all()
    print(f"그래프 트리플 수: {len(g):,}")
    for title, q in QUERIES.items():
        print_rows(title, g.query(q))


if __name__ == "__main__":
    main()
