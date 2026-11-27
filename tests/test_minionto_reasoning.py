"""30장. OWL 추론 결과를 업무에 쓰기 테스트 (추론은 모듈에서 한 번, DB 변경은 롤백)

실행: uv run pytest -q tests/test_minionto_reasoning.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))

from rdflib import RDF, Graph, URIRef  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.reasoning import SYSTEM, infer, plan_segments, sync_segments  # noqa: E402
from minionto.semantic import Mapping  # noqa: E402

TX = "https://example.org/taxos#"
TAX = {"id": "A001", "role": "세무사", "type": "human"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}


@pytest.fixture(scope="module")
def ctx():
    m = Mapping.load(ROOT / "06-hybrid-ontology" / "taxos_mapping.yaml")
    c = OntologyClient.open(ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml", today="2027-01-18")
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        yield c, m, infer(c, m)            # 추론은 CustomerSegment 에 의존하지 않으므로 한 번만
    c.close()


@pytest.fixture
def tx(ctx):
    c, m, inf = ctx
    with c.conn.transaction(force_rollback=True):
        yield c, m, inf


def rule(c, *types):
    q = c.objects("Customer")
    for t in types:
        q = q.where({"some": {"link": "risk_events", "where": {"field": "event_type", "op": "eq", "value": t}}})
    return sorted(o.pk for o in q.all())


def test_inference_equals_rule_queries(ctx):
    c, m, inf = ctx
    by = {m.short(k): v for k, v in inf.members.items()}
    assert not inf.errors and inf.complete
    assert sorted(by["tx:LateFiler"]) == rule(c, "기한후신고") and len(by["tx:LateFiler"]) == 52
    assert sorted(by["tx:UnreachableCustomer"]) == rule(c, "연락두절")
    assert sorted(by["tx:RepeatRiskCustomer"]) == rule(c, "기한후신고", "가산세부과")
    assert by["tx:RepeatRiskCustomer"]["C0001"] == ["E0001", "E0002"]        # 근거 = 조건을 만족시킨 위험 이벤트


def test_inconsistency_blocks_everything(tx):
    c, m, _ = tx
    bad = Graph()
    bad.add((URIRef("https://example.org/taxos/data/customer/C0002"), RDF.type, URIRef(TX + "SimplifiedTaxpayer")))
    wrong = infer(c, m, extra=bad)
    assert wrong.errors and "Disjoint classes" in wrong.errors[0]
    with pytest.raises(ValueError, match="OWL 모순"):
        plan_segments(c, m, wrong)


def test_sync_through_system_actions_only(tx):
    c, m, inf = tx
    assert c.actions.candidates("create_priority_contact", actor=TAX).count() == 0    # 분류 전에는 후보 없음
    res = sync_segments(c, m, inf)
    assert len(res) == 99 and all(r["status"] == "completed" for r in res)
    n = c.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = 'assign_segment' "
                       "AND actor->>'id' = %s", (SYSTEM["id"],)).fetchone()["n"]
    assert n == 99
    assert plan_segments(c, m, inf) == []                                        # 다시 돌려도 할 일 없음
    assert c.actions.candidates("create_priority_contact", actor=TAX).count() > 0
    r = c.actions.apply("assign_segment", "C0001", actor=TAX, params={"segment": "반복위험", "inferred_by": "수동"})
    assert r.status == "denied"                                                  # 사람이 직접 붙이지 못한다


def test_human_release_is_respected_and_partial_graph_keeps(tx):
    c, m, inf = tx
    sync_segments(c, m, inf)
    seg = c.objects("CustomerSegment").where(customer_id="C0001", segment="반복위험").first()
    assert c.actions.apply("release_segment", seg.pk, actor=TAX, params={"reason": "해당 없음"}).status == "completed"
    plan = plan_segments(c, m, inf)
    assert plan == [{"op": "skip", "customer": "C0001", "segment": "반복위험", "evidence": "E0001,E0002",
                     "why": "A001 이(가) 해제함 — 근거가 그대로"}]
    part = infer(c, m, actor=STAFF)
    assert part.complete is False
    assert {p["op"] for p in plan_segments(c, m, part)} <= {"keep", "skip"}     # 일부만 보는 그래프로는 해제 없음
