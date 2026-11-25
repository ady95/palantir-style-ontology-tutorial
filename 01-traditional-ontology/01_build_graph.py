"""07장. CSV(운영 데이터) → RDF 지식그래프 변환

datasets/taxos/data/*.csv 를 읽어 taxos 온톨로지의 인스턴스(ABox)를 만들고
output/taxos_data.ttl 로 저장합니다.

일부러 명시하지 않는 사실:
- 고객에게 tx:Customer 타입을 붙이지 않는다      → rdfs:subClassOf 추론으로 채워짐
- 법인에게 tx:GeneralTaxpayer 타입을 붙이지 않는다 → "법인은 일반과세자" 공리로 추론됨
- tx:manages(담당 고객)를 넣지 않는다             → owl:inverseOf 추론으로 채워짐

실행: uv run python 01-traditional-ontology/01_build_graph.py
"""

from __future__ import annotations

import csv
import time

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from common import DATA_DIR, DATA_GRAPH_FILE, OUTPUT_DIR, TX, TXD, bind

RISK_EVENT_CLASS = {
    "기한후신고": TX.LateFiling,
    "가산세부과": TX.PenaltyTax,
    "연락두절": TX.NoContact,
    "자료지연제출": TX.LateSubmission,
}


def read(table: str) -> list[dict]:
    with open(DATA_DIR / f"{table}.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def node(kind: str, key: str) -> URIRef:
    return TXD[f"{kind}/{key}"]


def lit(value: str, dtype=None):
    if value == "" or value is None:
        return None
    return Literal(value, datatype=dtype) if dtype else Literal(value)


def add(g: Graph, s, p, o) -> None:
    if o is not None:
        g.add((s, p, o))


def build() -> Graph:
    g = bind(Graph())

    for a in read("accountants"):
        s = node("accountant", a["accountant_id"])
        g.add((s, RDF.type, TX.TaxAccountant if a["role"] == "세무사" else TX.Staff))
        add(g, s, TX.id, lit(a["accountant_id"]))
        add(g, s, TX.name, lit(a["name"]))
        add(g, s, RDFS.label, lit(a["name"]))
        add(g, s, TX.team, lit(a["team"]))
        add(g, s, TX.maxWorkload, lit(a["max_workload"], XSD.integer))

    for c in read("customers"):
        s = node("customer", c["customer_id"])
        if c["business_type"] == "법인":
            g.add((s, RDF.type, TX.Corporation))          # 일반과세자는 추론에 맡긴다
        else:
            g.add((s, RDF.type, TX.IndividualBusiness))
            g.add((s, RDF.type, TX.SimplifiedTaxpayer if c["vat_type"] == "간이과세자" else TX.GeneralTaxpayer))
        add(g, s, TX.id, lit(c["customer_id"]))
        add(g, s, TX.name, lit(c["company_name"]))
        add(g, s, RDFS.label, lit(c["company_name"]))
        add(g, s, TX.businessRegNo, lit(c["business_reg_no"]))
        add(g, s, TX.industry, lit(c["industry"]))
        add(g, s, TX.businessStatus, lit(c["status"]))
        add(g, s, TX.riskLevel, lit(c["risk_level"]))
        g.add((s, TX.managedBy, node("accountant", c["accountant_id"])))

    for d in read("document_types"):
        s = node("doctype", d["doc_type_code"])
        g.add((s, RDF.type, TX.DocumentType))
        add(g, s, TX.id, lit(d["doc_type_code"]))
        add(g, s, TX.name, lit(d["doc_name"]))
        add(g, s, RDFS.label, lit(d["doc_name"]))

    for r in read("tax_returns"):
        s = node("return", r["return_id"])
        g.add((s, RDF.type, TX.TaxReturn))
        g.add((node("customer", r["customer_id"]), TX.hasTaxReturn, s))
        add(g, s, TX.id, lit(r["return_id"]))
        add(g, s, TX.taxKind, lit(r["tax_kind"]))
        add(g, s, TX.period, lit(r["period"]))
        add(g, s, TX.dueDate, lit(r["due_date"], XSD.date))
        add(g, s, TX.returnStatus, lit(r["status"]))
        add(g, s, TX.filedAt, lit(r["filed_at"], XSD.date))

    for d in read("document_submissions"):
        s = node("submission", d["submission_id"])
        g.add((s, RDF.type, TX.DocumentSubmission))
        g.add((node("return", d["return_id"]), TX.hasSubmission, s))
        g.add((s, TX.documentType, node("doctype", d["doc_type_code"])))
        add(g, s, TX.submissionStatus, lit(d["status"]))
        add(g, s, TX.requestedAt, lit(d["requested_at"], XSD.date))
        add(g, s, TX.submittedAt, lit(d["submitted_at"], XSD.date))

    for e in read("risk_events"):
        s = node("risk", e["event_id"])
        g.add((s, RDF.type, RISK_EVENT_CLASS[e["event_type"]]))
        g.add((node("customer", e["customer_id"]), TX.hasRiskEvent, s))
        add(g, s, TX.occurredAt, lit(e["occurred_at"], XSD.date))
        add(g, s, RDFS.comment, lit(e["note"]))

    for h in read("consultations"):
        s = node("consultation", h["consultation_id"])
        g.add((s, RDF.type, TX.Consultation))
        g.add((node("customer", h["customer_id"]), TX.hasConsultation, s))
        g.add((s, TX.handledBy, node("accountant", h["accountant_id"])))
        add(g, s, TX.topic, lit(h["topic"]))
        add(g, s, TX.summary, lit(h["summary"]))
        add(g, s, TX.occurredAt, lit(h["consulted_at"][:10], XSD.date))

    for t in read("tasks"):
        s = node("task", t["task_id"])
        g.add((s, RDF.type, TX.Task))
        g.add((node("customer", t["customer_id"]), TX.hasTask, s))
        if t["return_id"]:
            g.add((s, TX.aboutReturn, node("return", t["return_id"])))
        g.add((s, TX.assignedTo, node("accountant", t["assignee_id"])))
        add(g, s, TX.taskType, lit(t["task_type"]))
        add(g, s, TX.taskStatus, lit(t["status"]))
        add(g, s, TX.dueDate, lit(t["due_date"], XSD.date))

    return g


def main() -> None:
    t0 = time.perf_counter()
    g = build()
    OUTPUT_DIR.mkdir(exist_ok=True)
    g.serialize(DATA_GRAPH_FILE, format="turtle")
    print(f"트리플 {len(g):,}개 → {DATA_GRAPH_FILE.relative_to(DATA_GRAPH_FILE.parents[2])}"
          f"  ({time.perf_counter() - t0:.1f}초)")


if __name__ == "__main__":
    main()
