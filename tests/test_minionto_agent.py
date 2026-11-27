"""25장. AI 에이전트 인터페이스 테스트 (LLM 없이 도구를 직접 부른다)

실행: uv run pytest -q tests/test_minionto_agent.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402
from minionto.describe import public_metadata  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
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
        yield c
    c.close()


def pick(c, risk_op: str) -> str:
    return c.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": risk_op, "value": "높음"}).order_by("return_id").first().pk


def test_context_shows_only_what_the_actor_can_use(tx):
    s = AgentSession(tx, AI)
    ctx = s.context()
    assert set(ctx["actions"]) == {REQ, "start_review", "answer_inquiry", "escalate_inquiry"}   # AI비서가 할 수 있는 것만 (33장: 문의 2개)
    assert "phone" not in ctx["object_types"]["Customer"]["properties"]
    assert "phone" in ctx["object_types"]["Customer"]["hidden"]
    text = s.context_text()
    actions_part = text[text.index("# 실행할 수 있는 Action"):]
    assert "complete_filing" not in actions_part                   # 권한 밖 Action 은 실행 목록에 없다
    assert "complete_filing 검토중→신고완료" in text                 # 상태 전이(업무 흐름)로는 보인다
    assert "tax_returns " not in text and "primary_key" not in text  # 물리 매핑(테이블) 없음
    assert len(text) < len(json.dumps(public_metadata(tx.onto), ensure_ascii=False)) / 3
    names = [t["function"]["name"] for t in s.tools()]
    assert names == ["find_objects", "get_object", "call_function", "list_actions", "check_action", "run_action"]
    per = [t["function"]["name"] for t in s.tools("per_action")]
    assert per[-4:] == [REQ, "start_review", "answer_inquiry", "escalate_inquiry"]
    with pytest.raises(ValueError):
        AgentSession(tx, TAX)                                       # 에이전트 실행자 전용


def test_read_before_write_and_idempotent_run(tx):
    s = AgentSession(tx, AI, task_id="t1")
    ret = pick(tx, "ne")
    assert s.call("run_action", {"action": REQ, "target": ret})["status"] == "read_first"
    assert s.call("get_object", {"object_type": "TaxReturn", "id": ret})["object"]["status"] == "자료수집중"
    assert s.call("check_action", {"action": REQ, "target": ret})["status"] == "ready"
    prev = s.call("run_action", {"action": REQ, "target": ret, "dry_run": True})
    assert prev["status"] == "preview" and prev["changes"]
    first = s.call("run_action", {"action": REQ, "target": ret})
    assert first["status"] == "completed" and first["idempotency_key"] == f"agent:t1:{REQ}:{ret}"
    again = s.call("run_action", {"action": REQ, "target": ret})
    assert again["status"] == "replayed" and again["execution_id"] == first["execution_id"]
    n = tx.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = %s AND target_pk = %s "
                        "AND status = 'completed'", (REQ, ret)).fetchone()["n"]
    assert n == 1
    # 다른 업무(task)는 다른 키 → 엔진의 실행 조건(진행 중인 자료요청 업무)이 막는다
    other = AgentSession(tx, AI, task_id="t2")
    other.call("get_object", {"object_type": "TaxReturn", "id": ret})
    assert other.call("run_action", {"action": REQ, "target": ret})["status"] == "rejected"


def test_failures_carry_next_steps(tx):
    s = AgentSession(tx, AI)
    ret = pick(tx, "ne")
    s.call("get_object", {"object_type": "TaxReturn", "id": ret})
    denied = s.call("run_action", {"action": "complete_filing", "target": ret})
    assert denied["status"] == "denied" and "우회하지 말고" in denied["next_step"]
    rejected = s.call("check_action", {"action": "start_review", "target": ret})
    assert rejected["status"] == "rejected" and rejected["messages"]
    hidden = s.call("find_objects", {"object_type": "Customer", "where": [{"field": "phone", "op": "not_null"}]})
    assert hidden["status"] == "denied"
    bad = s.call("find_objects", {"object_type": "Nope"})
    assert bad["status"] == "invalid"
    assert s.call("no_such_tool", {})["status"] == "invalid"
    assert s.call("get_object", "{not json")["status"] == "invalid"
    risky = pick(tx, "eq")
    s.call("get_object", {"object_type": "TaxReturn", "id": risky})
    chk = s.call("check_action", {"action": REQ, "target": risky})
    assert chk["status"] == "approval_required" and "run_action" in chk["next_step"]   # 판단 단계의 안내
    run = s.call("run_action", {"action": REQ, "target": risky})
    assert run["status"] == "approval_required" and run["approval_id"]
    assert "반복하지 말고" in run["next_step"]                                       # 실행 단계의 안내
    assert s.call("run_action", {"action": REQ, "target": risky})["status"] == "replayed"
    assert len(tx.actions.pending_approvals()) == 1


def test_stale_read_is_rejected(tx):
    s = AgentSession(tx, AI)
    ret = "R00548"                                                     # 자료가 모두 들어온 신고를 만든다
    tx.conn.execute("UPDATE document_submissions SET status = '제출완료', submitted_at = '2027-01-17' "
                    "WHERE return_id = %s AND status IN ('미요청', '요청됨')", (ret,))
    s.call("get_object", {"object_type": "TaxReturn", "id": ret})
    assert tx.actions.apply("start_review", ret, actor=TAX).ok         # 그사이 사람이 먼저
    out = s.call("run_action", {"action": "start_review", "target": ret})
    assert out["status"] == "stale" and "get_object" in out["next_step"]
    s.call("get_object", {"object_type": "TaxReturn", "id": ret})        # 다시 읽으면 새 상태로 판단
    assert s.call("run_action", {"action": "start_review", "target": ret})["status"] == "rejected"


def test_find_objects_limits_rows_and_supports_link_quantifiers(tx):
    s = AgentSession(tx, AI, max_rows=5)
    out = s.call("find_objects", {"object_type": "TaxReturn", "limit": 100,
                                  "where": [{"field": "status", "op": "eq", "value": "자료수집중"}]})
    assert out["returned"] == 5 and out["count"] > 5 and "직접 세지 마세요" in out["note"]
    q = s.call("find_objects", {"object_type": "Customer", "where": [
        {"field": "risk_level", "op": "eq", "value": "높음"},
        {"some": {"link": "tax_returns", "where": {"field": "status", "op": "eq", "value": "자료수집중"}}}]})
    truth = tx.conn.execute("""SELECT count(DISTINCT c.customer_id) AS n FROM customers c
        JOIN tax_returns t USING (customer_id) WHERE c.risk_level = '높음' AND t.status = '자료수집중'""").fetchone()["n"]
    assert q["count"] == truth
    assert all("phone" not in r for r in q["rows"])
    fn = s.call("call_function", {"name": "filing_delay_risk", "args": {"customer": q["rows"][0]["customer_id"]}})
    assert fn["status"] == "ok" and "score" in fn["result"]


def test_confirm_mode_needs_a_human(tx):
    s = AgentSession(tx, AI, confirm=True)
    ret = pick(tx, "ne")
    s.call("get_object", {"object_type": "TaxReturn", "id": ret})
    out = s.call("run_action", {"action": REQ, "target": ret})
    assert out["status"] == "confirmation_required" and out["changes"]
    assert s.call("run_action", {"action": REQ, "target": ret})["token"] == out["token"]   # 같은 요청은 같은 토큰
    assert "confirm" not in [t["function"]["name"] for t in s.tools()]
    st = tx.get("DocumentSubmission", out["changes"][0]["pk"]).status
    assert st == "미요청"                                                # 확인 전에는 바뀌지 않았다
    assert s.confirm(out["token"])["status"] == "completed"
    assert tx.get("DocumentSubmission", out["changes"][0]["pk"]).status == "요청됨"
    assert s.confirm(out["token"])["status"] == "invalid"               # 토큰은 한 번만


def test_call_budget(tx):
    s = AgentSession(tx, AI, max_calls=2)
    s.call("find_objects", {"object_type": "Accountant", "limit": 1})
    s.call("find_objects", {"object_type": "Accountant", "limit": 1})
    assert s.call("find_objects", {"object_type": "Accountant"})["status"] == "budget_exceeded"
