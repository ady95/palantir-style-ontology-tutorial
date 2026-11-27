"""27장. 업무 그래프(LangGraph) 테스트 — 분기, 승인 대기 중단·재개, 중단 뒤 다시 실행 (바깥 트랜잭션은 롤백)

실행: uv run pytest -q tests/test_minionto_workflow.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

pytest.importorskip("langgraph")
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.workflow import REQ, FilingRiskWorkflow, check_note, check_summary, config, interrupts  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}


@pytest.fixture
def tx():
    c = OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        yield c
    c.close()


def start(tx, task="t1", saver=None, **kw):
    saver = saver or InMemorySaver()
    g = FilingRiskWorkflow(tx, AI, **kw).build(checkpointer=saver)
    out = g.invoke({"task_id": task, "as_of": AS_OF}, config(task))
    return g, saver, out


def by_status(state) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for it in state["items"]:
        out.setdefault(it["status"], []).append(it["return_id"])
    return out


def log_count(tx, ret) -> int:
    return tx.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = %s AND target_pk = %s "
                           "AND status = 'completed'", (REQ, ret)).fetchone()["n"]


def test_graph_branches_and_stops_for_approval(tx):
    g, _, out = start(tx)
    st = by_status(out)
    assert len(out["items"]) == 37                                      # 위험 점수 50 이상
    assert len(st["completed"]) == 5 and len(st["approval_required"]) == 2 and len(st["handoff"]) == 30
    (wait,) = interrupts(out)                                           # 승인 대기에서 멈췄다
    assert {w["return_id"] for w in wait["waiting"]} == set(st["approval_required"])
    done = [i for i in out["items"] if i["status"] == "completed"]
    assert all(i["idempotency_key"] == f"agent:t1:{REQ}:{i['return_id']}" for i in done)
    assert all(i["note_by"] == "template" and i["note"] for i in done)
    handoff = [i for i in out["items"] if i["status"] == "handoff"]
    assert all("note" not in i for i in handoff)                       # 할 수 없는 일에는 메모도 쓰지 않는다
    assert any("휴업·폐업" in m for i in handoff for m in i["messages"])
    assert "report" not in out


def test_resume_reads_approvals_from_the_engine(tx):
    g, _, out = start(tx)
    a, b = interrupts(out)[0]["waiting"]
    again = g.invoke(Command(resume={"approved": True}), config("t1"))  # 재개 값만으로는 아무것도 바뀌지 않는다
    assert [w["approval_id"] for w in interrupts(again)[0]["waiting"]] == [a["approval_id"], b["approval_id"]]
    assert tx.actions.decide(a["approval_id"], TAX).status == "completed"
    tx.actions.decide(b["approval_id"], TAX, approve=False, comment="고객과 통화 후 결정")
    final = g.invoke(Command(resume="check"), config("t1"))
    items = {i["return_id"]: i for i in final["items"]}
    assert items[a["return_id"]]["status"] == "completed" and items[a["return_id"]]["verified"] is True
    assert items[a["return_id"]]["decided_by"] == "A001"
    assert items[b["return_id"]]["status"] == "rejected_by_approver"
    assert final["report"]["counts"] == {"completed": 6, "handoff": 30, "rejected_by_approver": 1}
    assert final["report"]["unverified"] == []


def test_stop_waiting_leaves_the_request(tx):
    g, _, out = start(tx)
    final = g.invoke(Command(resume={"stop": True}), config("t1"))
    assert final["report"]["counts"]["still_waiting"] == 2
    assert len(tx.actions.pending_approvals()) == 2                    # 승인 요청은 엔진에 그대로 남는다


def test_crash_after_act_does_not_run_twice(tx):
    crashed = []

    def crash_once(item, out):
        if out["status"] == "completed" and not crashed:
            crashed.append(item["return_id"])
            raise RuntimeError("프로세스가 죽었다고 치자")
    saver = InMemorySaver()
    wf = FilingRiskWorkflow(tx, AI, on_act=crash_once)
    g = wf.build(checkpointer=saver)
    with pytest.raises(RuntimeError):
        g.invoke({"task_id": "t2", "as_of": AS_OF}, config("t2"))
    ret = crashed[0]
    assert log_count(tx, ret) == 1                                     # 실행은 됐고, 그래프는 모른다
    g2 = FilingRiskWorkflow(tx, AI).build(checkpointer=saver)          # 새 프로세스라고 치자
    out = g2.invoke(None, config("t2"))                                # 마지막 체크포인트부터 다시
    it = next(i for i in out["items"] if i["return_id"] == ret)
    assert it["status"] == "completed" and it["replayed"] is True
    assert log_count(tx, ret) == 1                                     # 두 번 실행되지 않았다
    assert len(by_status(out)["completed"]) == 5


def test_change_between_check_and_act_is_caught(tx):
    saver = InMemorySaver()
    g = FilingRiskWorkflow(tx, AI).build(checkpointer=saver, interrupt_before=["act"])
    out = g.invoke({"task_id": "t3", "as_of": AS_OF}, config("t3"))
    it = out["items"][out["cursor"]]
    tx.conn.execute("UPDATE tax_returns SET status = '검토중' WHERE return_id = %s", (it["return_id"],))
    out = g.invoke(None, config("t3"))                                 # act → stale → analyze → 넘김
    after = next(i for i in out["items"] if i["return_id"] == it["return_id"])
    assert after["status"] == "handoff" and after["check"] == "rejected"
    assert [x["node"] for x in out["log"] if x.get("return_id") == it["return_id"]] == \
        ["analyze", "draft", "act", "analyze"]
    assert log_count(tx, it["return_id"]) == 0


def test_check_note():
    item = {"missing": ["부가세 매입 세금계산서"]}
    assert check_note("1월 25일까지 부가세 매입 세금계산서를 보내 주세요.", item) == []
    assert "요청할 자료 이름이 없음" in check_note("자료를 보내 주세요.", item)
    assert "주지 않은 전화번호·금액" in check_note("부가세 매입 세금계산서, 문의 02-123-4567", item)
    assert check_note("가" * 201 + "부가세 매입 세금계산서", item)[0].startswith("200자 초과")
    report = {"completed": ["R00146"], "handoff_by_reason": {"사유": ["R00024"]}}
    assert check_summary("R00146 완료, R00024 넘김", report) == []
    assert check_summary("R00146, R09999 완료", report) == ["보고에 없는 ID R09999"]
