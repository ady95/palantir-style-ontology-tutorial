"""32장. Northwind 발주 업무 테스트 — 값 Function(되쓰기), 금액별 승인, 검증기, 발주 그래프 (롤백)

실행: uv run pytest -q tests/test_northwind_purchasing.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(ROOT / "07-northwind-project"))

from minionto import OntologyClient  # noqa: E402
from minionto.metamodel import Ontology  # noqa: E402
from minionto.validator import validate  # noqa: E402

NW = ROOT / "03-ontology-engine" / "ontologies" / "northwind" / "ontology.yaml"
SALES = {"id": "E-sales", "role": "영업담당", "type": "human"}
BUYER = {"id": "E-buyer", "role": "구매담당", "type": "human"}
BOSS = {"id": "E-boss", "role": "관리자", "type": "human"}
AI = {"id": "agent-nw", "role": "AI비서", "type": "agent"}


@pytest.fixture
def c():
    cl = OntologyClient.open(NW, today="2026-12-01")
    try:
        cl.conn
    except Exception:  # noqa: BLE001
        pytest.skip("Northwind DB에 연결할 수 없습니다")
    with cl.conn.transaction(force_rollback=True):
        cl.actions.ensure_tables()
        yield cl
    cl.close()


def test_write_back_through_value_functions(c):
    p = c.get("Product", 30)                                  # 재고 10, 예정 0, 기준 15 → 부족
    r = c.actions.apply("request_purchase", 30, actor=BUYER, params={"quantity": 20})
    assert r.status == "completed"
    po = c.objects("PurchaseOrder").where(product_id=30).first()
    assert po.amount == Decimal(str(p.unit_price)) * 20 and po.status == "발주완료"
    assert c.get("Product", 30).units_on_order == 20
    assert c.actions.apply("receive_purchase", po.pk, actor=BUYER, params={"received_quantity": 18}).status == "completed"
    after = c.get("Product", 30)
    assert after.units_in_stock == p.units_in_stock + 18 and after.units_on_order == 0
    assert c.actions.apply("record_sale", 30, actor=SALES, params={"quantity": 5}).status == "completed"
    assert c.get("Product", 30).units_in_stock == p.units_in_stock + 13
    assert c.actions.apply("record_sale", 30, actor=SALES, params={"quantity": 999}).status == "rejected"


def test_amount_based_approval(c):
    # 상품 70: 단가 15 · 상품 30: 단가 25.89
    assert c.actions.check("request_purchase", 70, actor=BUYER, params={"quantity": 35}).status == "ready"      # 525
    assert c.actions.check("request_purchase", 70, actor=BUYER, params={"quantity": 70}).status == "approval_required"  # 1050
    assert c.actions.check("request_purchase", 70, actor=AI, params={"quantity": 19}).status == "ready"          # 285
    assert c.actions.check("request_purchase", 70, actor=AI, params={"quantity": 20}).status == "approval_required"  # 300
    r = c.actions.apply("request_purchase", 70, actor=AI, params={"quantity": 35})
    assert r.status == "approval_required" and c.get("Product", 70).units_on_order == 10       # 승인 전에는 그대로
    assert c.actions.decide(r.approval_id, BOSS).status == "completed"
    assert c.get("Product", 70).units_on_order == 45


def test_cancel_restores_on_order(c):
    c.actions.apply("request_purchase", 30, actor=BUYER, params={"quantity": 20})
    po = c.objects("PurchaseOrder").where(product_id=30).first()
    assert c.actions.apply("cancel_purchase", po.pk, actor=BUYER, params={"reason": "공급업체 품절"}).status == "completed"
    assert c.get("Product", 30).units_on_order == 0
    assert c.actions.apply("receive_purchase", po.pk, actor=BUYER, params={"received_quantity": 1}).status == "rejected"


def test_validator_checks_value_functions():
    raw = yaml.safe_load(NW.read_text(encoding="utf-8"))
    edits = raw["actions"]["request_purchase"]["edits"]
    edits[0]["values"]["amount"] = {"function": "no_such_function", "args": {}}
    edits[1]["set"]["units_on_order"] = {"function": "on_order_after", "args": {"product": "$target.product_id",
                                                                               "qty": "$params.quantity"}}
    raw["actions"]["receive_purchase"]["edits"][1]["set"]["units_in_stock"] = {
        "function": "purchase_amount_at_least", "args": {"product": "$target.product_id", "quantity": 1, "limit": 1}}
    rep = validate(Ontology.model_validate(raw))
    msgs = [e.message for e in rep.errors]
    assert any("'no_such_function' 이 없습니다" in m for m in msgs)
    assert any("입력 'qty' 이 없습니다" in m for m in msgs) and any("필수 입력이 빠졌습니다: quantity" in m for m in msgs)
    assert any("boolean 를 돌려주는데 속성은 integer" in m for m in msgs)


def test_purchasing_graph(c):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from purchasing import PurchasingWorkflow, config
    c.actions.apply("record_sale", 54, actor=SALES, params={"quantity": 12})          # 재고 21 → 9 (기준 10)
    saver = InMemorySaver()
    out = PurchasingWorkflow(c, AI).build(checkpointer=saver).invoke({"task_id": "T1"}, config("T1"))
    st = {i["product_id"]: i["status"] for i in out["items"]}
    assert st == {30: "approval_required", 54: "completed", 70: "approval_required"}
    for w in out["__interrupt__"][0].value["waiting"]:
        c.actions.decide(w["approval_id"], BOSS, approve=w["product_id"] == 30)
    final = PurchasingWorkflow(c, AI).build(checkpointer=saver).invoke(Command(resume="check"), config("T1"))
    assert final["report"]["counts"] == {"completed": 2, "rejected_by_approver": 1}
