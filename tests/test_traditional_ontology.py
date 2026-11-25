"""PART 2 실습 회귀 테스트

기대값은 CSV 원본에서 직접 계산해, SPARQL·추론·SHACL 결과가 운영 데이터와 일치하는지 확인한다.
실행: uv run pytest -q
"""

from __future__ import annotations

import csv
import importlib
import shutil
import sys
from pathlib import Path

import pytest

PART2 = Path(__file__).resolve().parent.parent / "01-traditional-ontology"
sys.path.insert(0, str(PART2))

common = importlib.import_module("common")
build = importlib.import_module("01_build_graph")
sparql = importlib.import_module("02_sparql_queries")
owlrl_mod = importlib.import_module("03_reasoning_owlrl")
shacl = importlib.import_module("05_shacl_validation")


def read(table: str) -> list[dict]:
    with open(common.DATA_DIR / f"{table}.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def graph():
    build.main()
    return common.load_all()


@pytest.fixture(scope="module")
def expected():
    customers = {c["customer_id"]: c for c in read("customers")}
    returns = read("tax_returns")
    subs = read("document_submissions")
    current = {r["return_id"]: r for r in returns if r["due_date"] == common.CURRENT_DUE}
    missing_returns = {s["return_id"] for s in subs if s["status"] in ("미요청", "요청됨")}
    targets = {current[rid]["customer_id"] for rid in missing_returns
               if rid in current and current[rid]["status"] == "자료수집중"
               and customers[current[rid]["customer_id"]]["status"] == "영업"}
    with_subs = {s["return_id"] for s in subs}
    late = {e["customer_id"] for e in read("risk_events") if e["event_type"] == "기한후신고"}
    return {
        "targets": targets,
        "unregistered": {r["customer_id"] for rid, r in current.items() if rid not in with_subs},
        "late_filers": late,
        "corporations": sum(1 for c in customers.values() if c["business_type"] == "법인"),
        "customers": len(customers),
    }


def test_missing_documents_query(graph, expected):
    rows = list(graph.query(sparql.Q2_MISSING))
    assert {str(r[0]) for r in rows} == expected["targets"]


def test_unregistered_query(graph, expected):
    rows = list(graph.query(sparql.Q3_UNREGISTERED))
    assert {str(r[0]) for r in rows} == expected["unregistered"]


def test_workload_query(graph):
    rows = list(graph.query(sparql.Q4_WORKLOAD))
    assert all(int(r[3]) > int(r[2]) for r in rows) and rows


def test_owlrl_inference(expected):
    g = owlrl_mod.reason(common.load_all())
    assert owlrl_mod.count(g, owlrl_mod.CHECKS["tx:Customer 인스턴스"]) == expected["customers"]
    assert owlrl_mod.count(g, owlrl_mod.CHECKS["  └ 그중 법인"]) == expected["corporations"]
    assert owlrl_mod.count(g, owlrl_mod.CHECKS["tx:manages 관계"]) == expected["customers"]
    assert owlrl_mod.count(g, owlrl_mod.CHECKS["tx:LateFiler (기한후신고 이력 고객)"]) == len(expected["late_filers"])


def test_shacl_clean_data_only_warnings(graph, expected):
    conforms, results = shacl.run(common.load_data())
    assert conforms
    assert [r[0] for r in results] == ["Warning"] * len(expected["unregistered"])


def test_shacl_detects_broken_data():
    conforms, results = shacl.run(shacl.break_data(common.load_data()))
    assert not conforms
    assert sum(1 for r in results if r[0] == "Violation") == 4


@pytest.mark.skipif(shutil.which("java") is None, reason="HermiT 실행에 Java가 필요합니다")
def test_hermit_detects_inconsistency(tmp_path, capsys):
    hermit = importlib.import_module("04_reasoning_hermit")
    hermit.case_inconsistent(tmp_path)
    assert "모순 검출" in capsys.readouterr().out
