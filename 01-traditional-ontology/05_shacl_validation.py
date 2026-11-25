"""08장(4). SHACL 데이터 검증 (pySHACL)

1) 정상 데이터 검증: 위반(Violation)은 0건, 자료 현황 미등록 신고는 경고(Warning)로 보고된다.
2) 일부러 망가뜨린 데이터 검증: 각 규칙이 어떤 메시지로 위반을 보고하는지 확인한다.

실행: uv run python 01-traditional-ontology/05_shacl_validation.py
"""

from __future__ import annotations

from collections import Counter

from pyshacl import validate
from rdflib import Graph, Literal
from rdflib.namespace import RDF, SH

from common import SHAPES_FILE, TX, TXD, load_data, load_ontology


def run(data: Graph) -> tuple[bool, list[tuple[str, str, str]]]:
    conforms, report, _ = validate(
        data,
        shacl_graph=Graph().parse(SHAPES_FILE),
        ont_graph=load_ontology(),     # rdfs:subClassOf 를 반영해 targetClass·sh:class 판단
        inference="none",
        allow_warnings=True,           # 경고만 있으면 conforms=True
        advanced=True,
    )
    results = []
    for r in report.subjects(RDF.type, SH.ValidationResult):
        sev = report.value(r, SH.resultSeverity).split("#")[-1]
        focus = str(report.value(r, SH.focusNode)).rsplit("/", 1)[-1]
        msg = str(report.value(r, SH.resultMessage) or "")
        results.append((sev, focus, msg))
    return conforms, sorted(results)


def show(title: str, conforms: bool, results) -> None:
    print(f"\n## {title}")
    print(f"  conforms={conforms}  " + ", ".join(f"{k} {v}건" for k, v in Counter(r[0] for r in results).items()))
    for sev, focus, msg in results[:12]:
        print(f"  [{sev}] {focus}: {msg}")


def break_data(g: Graph) -> Graph:
    c1 = TXD["customer/C0001"]
    g.add((c1, RDF.type, TX.Corporation))
    g.add((c1, RDF.type, TX.SimplifiedTaxpayer))                     # 법인 + 간이과세자
    g.set((TXD["customer/C0002"], TX.riskLevel, Literal("매우높음")))  # 허용되지 않은 값
    g.remove((TXD["customer/C0003"], TX.managedBy, None))             # 담당자 누락
    sub = next(g.subjects(TX.submissionStatus, Literal("제출완료")))
    g.remove((sub, TX.submittedAt, None))                             # 제출일 누락
    return g


def main() -> None:
    show("1) 정상 데이터", *run(load_data()))
    show("2) 일부러 망가뜨린 데이터", *run(break_data(load_data())))


if __name__ == "__main__":
    main()
