"""31장. 네 방식 비교 — 구현이 같은 답을 내는지, 규칙 변경이 어디까지 닿는지 (DB 변경은 롤백)

실행: uv run pytest -q tests/test_approach_comparison.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent / "10-benchmarks" / "approach_comparison"
sys.path.insert(0, str(HERE))

import bench  # noqa: E402,F401
import a_sql  # noqa: E402
import b_rdf  # noqa: E402
import c_onto  # noqa: E402
from bench import AI, AS_OF, DUE, REQUEST_CASES, STAFF, TAXOS  # noqa: E402
from minionto import OntologyClient  # noqa: E402


@pytest.fixture(scope="module")
def graph():
    return b_rdf.load_graph()


@pytest.fixture
def c():
    cl = OntologyClient.open(TAXOS, today=AS_OF)
    try:
        cl.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with cl.conn.transaction(force_rollback=True):
        cl.actions.ensure_tables()
        yield cl
    cl.close()


def test_same_answers(c, graph):
    a = a_sql.missing_customers(c.conn, AS_OF, DUE)
    assert len(a) == 92 and b_rdf.missing_customers(graph, AS_OF, DUE) == a == c_onto.missing_customers(c, AS_OF, DUE)
    assert a_sql.repeat_risk(c.conn) == c_onto.repeat_risk(c) and len(a_sql.repeat_risk(c.conn)) == 39


def test_request_rules_in_every_approach(c, graph):
    for ret, want in REQUEST_CASES.items():
        with c.conn.transaction(force_rollback=True):
            assert a_sql.request_documents(c.conn, ret, AI)["status"] == want, ("A", ret)
        with c.conn.transaction(force_rollback=True):
            assert c_onto.request_documents(c, ret, AI)["status"] == want, ("C", ret)
        g = b_rdf.Graph()
        g += graph
        assert b_rdf.request_documents(g, ret, AI, AS_OF)["status"] == want, ("B", ret)


def test_graph_execution_does_not_reach_the_db(c, graph):
    g = b_rdf.Graph()
    g += graph
    before = c.objects("Task").count()
    assert b_rdf.request_documents(g, "R00146", AI, AS_OF)["status"] == "executed"
    assert b_rdf.task_count(g, "R00146") == 1 and c.objects("Task").count() == before


def test_permissions_hand_written_vs_policy(c):
    assert len(a_sql.list_customers(c.conn, STAFF)) == len(c_onto.list_customers(c, STAFF)) == 43
    assert all("phone" not in r for r in a_sql.list_customers(c.conn, AI))
    assert all(r.get("phone") is None for r in c_onto.list_customers(c, AI))
