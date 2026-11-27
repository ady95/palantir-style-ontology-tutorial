"""32장. 재고 부족 → 발주 업무 그래프 (27장의 방식을 Northwind 에)

    detect(부족 상품·제안 수량) → order(한 건씩 발주) → … → wait_approval(승인 대기, 중단·재개) → report

- 도구는 25장의 AgentSession — 실행자는 AI 에이전트(AI비서). 권한·실행 조건·금액별 승인은 Northwind 정의가 정한다.
- 업무 ID = thread_id = 도구 세션 키 = 멱등 키의 업무 부분 (27장과 같다).
- 승인 결과는 재개 값이 아니라 엔진의 승인 기록에서 읽는다.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from minionto.actions import ENGINE_SCHEMA
from minionto.agent import SessionStore

REQ = "request_purchase"


class State(TypedDict, total=False):
    task_id: str
    items: list[dict]
    cursor: int
    log: Annotated[list[dict], operator.add]
    report: dict


class PurchasingWorkflow:
    def __init__(self, client, actor: dict):
        self.client, self.actor = client, dict(actor)
        self.store = SessionStore(client)

    def _session(self, state: State):
        return self.store.open(state["task_id"], self.actor, max_rows=500, max_calls=10_000)

    # ------------------------------------------------------------------ 노드
    def detect(self, state: State) -> dict:
        """판매 중인 상품마다 is_low_stock · reorder_quantity · purchase_amount 를 부른다"""
        s = self._session(state)
        rows = s.call("find_objects", {"object_type": "Product", "order_by": "product_id",
                                       "where": [{"field": "discontinued", "op": "eq", "value": 0}]})["rows"]
        items = []
        for r in rows:
            pid = r["product_id"]
            if not s.call("call_function", {"name": "is_low_stock", "args": {"product": pid}}).get("result"):
                continue
            q = s.call("call_function", {"name": "reorder_quantity", "args": {"product": pid}})["result"]
            amt = s.call("call_function", {"name": "purchase_amount", "args": {"product": pid, "quantity": q}})["result"]
            items.append({"product_id": pid, "name": r["product_name"], "stock": r["units_in_stock"],
                          "on_order": r["units_on_order"], "reorder_level": r["reorder_level"],
                          "quantity": q, "amount": amt, "status": "new"})
        self.store.save(state["task_id"], s)
        return {"items": items, "cursor": 0, "log": [{"node": "detect", "products": len(rows), "low": len(items)}]}

    def order(self, state: State) -> dict:
        items, i = [dict(x) for x in state["items"]], state["cursor"]
        it = items[i]
        s = self._session(state)
        s.call("get_object", {"object_type": "Product", "id": str(it["product_id"])})
        out = s.call("run_action", {"action": REQ, "target": str(it["product_id"]),
                                    "params": {"quantity": it["quantity"], "note": "재고 부족 자동 발주"}})
        self.store.save(state["task_id"], s)
        status = out.get("original_status", out["status"]) if out["status"] == "replayed" else out["status"]
        it.update(status=status, approval_id=out.get("approval_id"), execution_id=out.get("execution_id"),
                  messages=out.get("messages", []))
        items[i] = it
        return {"items": items, "cursor": i + 1, "log": [{"node": "order", "product": it["product_id"], "status": status}]}

    def wait_approval(self, state: State) -> Command[Literal["wait_approval", "report"]]:
        items = self.reconcile(state["items"])
        waiting = [i for i in items if i["status"] == "approval_required"]
        if not waiting:
            return Command(update={"items": items}, goto="report")
        answer = interrupt({"task_id": state["task_id"], "waiting": [
            {"approval_id": i["approval_id"], "product_id": i["product_id"], "amount": i["amount"]} for i in waiting]})
        if isinstance(answer, dict) and answer.get("stop"):
            return Command(update={"items": items}, goto="report")
        return Command(update={"items": items}, goto="wait_approval")

    def reconcile(self, items: list[dict]) -> list[dict]:
        items = [dict(x) for x in items]
        for it in items:
            if it["status"] != "approval_required" or not it.get("approval_id"):
                continue
            row = self.client.conn.execute(f"SELECT status, execution_id FROM {ENGINE_SCHEMA}.approval_requests "
                                           "WHERE approval_id = %s", (it["approval_id"],)).fetchone()
            if row and row["status"] != "pending":
                it["status"] = {"approved": "completed", "rejected": "rejected_by_approver",
                                "expired": "expired"}[row["status"]]
                it["execution_id"] = str(row["execution_id"]) if row["execution_id"] else it.get("execution_id")
        return items

    def report(self, state: State) -> dict:
        counts: dict[str, int] = {}
        for it in state["items"]:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        total = sum(float(i["amount"]) for i in state["items"] if i["status"] == "completed")
        return {"report": {"counts": counts, "ordered_amount": round(total, 2),
                           "waiting": [i["product_id"] for i in state["items"] if i["status"] == "approval_required"]}}

    # ------------------------------------------------------------------ 조립
    @staticmethod
    def after_detect(state: State) -> str:
        return "order" if state["items"] else "report"

    @staticmethod
    def after_order(state: State) -> str:
        return "order" if state["cursor"] < len(state["items"]) else "wait_approval"

    def build(self, checkpointer=None):
        g = StateGraph(State)
        for n in ("detect", "order", "wait_approval", "report"):
            g.add_node(n, getattr(self, n))
        g.add_edge(START, "detect")
        g.add_conditional_edges("detect", self.after_detect, ["order", "report"])
        g.add_conditional_edges("order", self.after_order, ["order", "wait_approval"])
        g.add_edge("report", END)
        return g.compile(checkpointer=checkpointer)


def config(task_id: str) -> dict:
    return {"configurable": {"thread_id": task_id}, "recursion_limit": 1000}
