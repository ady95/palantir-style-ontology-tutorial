"""B. 전통적 온톨로지 — RDF·OWL·SPARQL·SHACL (PART 2)

운영 데이터를 RDF 로 변환(ETL)한 그래프 위에서 질의·추론·검증한다. 그래프는 기록 시스템이 아니므로,
업무 실행(SPARQL UPDATE)은 그래프만 바꾸고 운영 DB 에는 닿지 않는다.
"""

from __future__ import annotations

from importlib import import_module

import owlrl
from rdflib import Graph, URIRef

from bench import ROOT

PREFIX = """PREFIX tx: <https://example.org/taxos#>
PREFIX txd: <https://example.org/taxos/data/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""
TBOX = [ROOT / "01-traditional-ontology" / "ontology" / "taxos.ttl", ROOT / "06-hybrid-ontology" / "taxos_rules.ttl"]
SHAPES = ROOT / "01-traditional-ontology" / "shapes" / "taxos_shapes.ttl"
DATA = "https://example.org/taxos/data/"


def load_graph() -> Graph:
    """ETL: 07장 변환 코드(CSV → RDF) + TBox"""
    g = import_module("01_build_graph").build()
    for f in TBOX:
        g.parse(f)
    return g


# ---------------------------------------------------------------------- T1 조회
def missing_customers(g: Graph, as_of: str, due: str) -> list[str]:
    q = PREFIX + f"""SELECT DISTINCT ?cid WHERE {{
      ?c tx:hasTaxReturn ?r ; tx:id ?cid ; tx:businessStatus "영업" .
      ?r tx:dueDate ?due ; tx:returnStatus "자료수집중" ; tx:hasSubmission ?s .
      ?s tx:submissionStatus ?st . FILTER (?st IN ("미요청", "요청됨"))
      FILTER (?due >= "{as_of}"^^xsd:date && ?due <= "{due}"^^xsd:date)
    }} ORDER BY ?cid"""
    return [str(r.cid) for r in g.query(q)]


# ---------------------------------------------------------------------- T2 분류 (OWL-RL)
def repeat_risk(g: Graph) -> list[str]:
    closed = Graph()
    closed += g
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(closed)
    q = PREFIX + "SELECT ?cid WHERE { ?c a tx:RepeatRiskCustomer ; tx:id ?cid } ORDER BY ?cid"
    return [str(r.cid) for r in closed.query(q)]


# ---------------------------------------------------------------------- T3 검증 (SHACL)
def validate(g: Graph) -> tuple[int, int, list[str]]:
    """(위반, 경고, 메시지) — 그래프를 만든 뒤에 찾는다 (막지는 않는다)"""
    from pyshacl import validate as shacl
    _, report, _ = shacl(g, shacl_graph=Graph().parse(SHAPES), inference="none")
    q = """PREFIX sh: <http://www.w3.org/ns/shacl#>
           SELECT ?sev ?msg WHERE { ?r a sh:ValidationResult ; sh:resultSeverity ?sev ; sh:resultMessage ?msg }"""
    rows = [(str(s).rsplit("#", 1)[1], str(m)) for s, m in report.query(q)]
    return (sum(s == "Violation" for s, _ in rows), sum(s == "Warning" for s, _ in rows),
            sorted({m for _, m in rows}))


# ---------------------------------------------------------------------- T4 업무 실행 (SPARQL UPDATE)
def _ask(g: Graph, body: str, ret: str) -> bool:
    return bool(g.query(PREFIX + f"ASK {{ BIND(<{DATA}return/{ret}> AS ?r) {body} }}").askAnswer)


def request_documents(g: Graph, return_id: str, actor: dict, as_of: str, rule_v2: bool = False) -> dict:
    """규칙은 ASK 로 확인하고, 변경은 SPARQL UPDATE 로. 바뀌는 것은 그래프뿐이다
    rule_v2: T8 의 규칙 변경 — 바뀐 줄은 '# v2' 표시 2줄"""
    if not _ask(g, '?r tx:returnStatus "자료수집중" .', return_id):
        return {"status": "blocked", "reason": "자료수집중인 신고가 아님"}
    if not _ask(g, '?c tx:hasTaxReturn ?r ; tx:businessStatus "영업" .', return_id):
        return {"status": "blocked", "reason": "휴업·폐업 고객"}
    if _ask(g, '?c tx:hasTask ?t . ?t tx:aboutReturn ?r ; tx:taskType "자료요청" ; tx:taskStatus ?st . '
               'FILTER (?st IN ("대기", "진행중"))', return_id):
        return {"status": "blocked", "reason": "진행 중인 자료요청 업무"}
    if not _ask(g, '?r tx:hasSubmission ?s . ?s tx:submissionStatus "미요청" .', return_id):
        return {"status": "blocked", "reason": "미요청 자료 없음"}
    if actor["type"] == "agent" and _ask(g, '?c tx:hasTaxReturn ?r ; tx:riskLevel "높음" .', return_id):
        return {"status": "approval", "reason": "고위험 고객 — 세무사 승인 필요 (승인 기능 없음)"}
    if rule_v2 and actor["type"] == "agent" and _ask(g, "?c tx:hasTaxReturn ?r ; tx:hasRiskEvent/a tx:LateFiling .", return_id):  # v2
        return {"status": "approval", "reason": "기한후신고 이력 고객 — 세무사 승인 필요"}                                  # v2
    g.update(PREFIX + f"""
      DELETE {{ ?s tx:submissionStatus "미요청" }}
      INSERT {{ ?s tx:submissionStatus "요청됨" ; tx:requestedAt "{as_of}"^^xsd:date .
               ?c tx:hasTask ?t . ?t a tx:Task ; tx:aboutReturn ?r ; tx:taskType "자료요청" ; tx:taskStatus "진행중" }}
      WHERE {{ BIND(<{DATA}return/{return_id}> AS ?r) ?c tx:hasTaxReturn ?r . ?r tx:hasSubmission ?s .
               ?s tx:submissionStatus "미요청" . BIND(IRI(CONCAT(STR(?r), "/task-request")) AS ?t) }}""")
    return {"status": "executed", "note": "그래프만 바뀜 — 운영 DB 에는 반영되지 않음"}


def task_count(g: Graph, return_id: str) -> int:
    q = PREFIX + f'SELECT (COUNT(?t) AS ?n) WHERE {{ ?t tx:aboutReturn <{DATA}return/{return_id}> ; tx:taskType "자료요청" }}'
    return int(next(iter(g.query(q)))[0])


# ---------------------------------------------------------------------- T5·T6
def customers_visible_to(g: Graph, actor: dict) -> int | None:
    """표준(RDF·SPARQL)에는 실행자별 접근 제어가 없다 — 그래프를 가진 사람은 모두 본다"""
    return None


def node(kind: str, key: str) -> URIRef:
    return URIRef(f"{DATA}{kind}/{key}")
