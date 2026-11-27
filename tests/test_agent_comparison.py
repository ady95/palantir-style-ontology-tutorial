"""28장. 비교 실험 하네스 테스트 — LLM 없이 도구를 직접 불러 판정이 맞는지 확인 (바깥 트랜잭션은 롤백)

실행: uv run pytest -q tests/test_agent_comparison.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(ROOT / "10-benchmarks" / "agent_comparison"))

from agents import OntologyAgent, PlainAgent, SqlAgent, run_turns  # noqa: E402
from minionto import OntologyClient  # noqa: E402
from scenarios import BY_ID, audit_coverage, diff, executions_since, redact, snapshot  # noqa: E402

TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"


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


def judge(c, sid, calls, agent_cls, answers=("",)):
    """도구 호출을 직접 실행하고 시나리오 판정과 감사 기록 비율을 돌려준다"""
    sc = BY_ID[sid]
    with c.conn.transaction(force_rollback=True):
        if sc.setup:
            sc.setup(c)
        truth = sc.truth(c)
        known = {str(r["execution_id"]) for r in c.conn.execute("SELECT execution_id FROM minionto.action_log")}
        before = snapshot(c.conn)
        agent = agent_cls(c, AS_OF, f"t-{sid}")
        outs = [agent.call(n, a) for n, a in calls]
        d = diff(before, snapshot(c.conn))
        ok, note = sc.check({"answers": list(answers), "calls": []}, d, truth)
        return ok, note, audit_coverage(d, executions_since(c.conn, known)), outs


def test_rule_violation_is_judged_by_db_change(tx):
    ok, note, _, outs = judge(tx, "N3", [("request_missing_documents", {"return_id": "R00360"})], PlainAgent)
    assert outs[0]["ok"] is True and ok is False                    # 일반 도구는 휴업 고객도 그대로 실행
    ok, note, _, outs = judge(tx, "N3", [("get_object", {"object_type": "TaxReturn", "id": "R00360"}),
                                         ("run_action", {"action": "request_missing_documents",
                                                         "target": "R00360"})], OntologyAgent)
    assert outs[1]["status"] == "rejected" and ok is True and note == "변경 없음"


def test_task_success_and_audit_coverage(tx):
    ok, note, cov, _ = judge(tx, "W1", [("request_missing_documents", {"return_id": "R00146"})], PlainAgent)
    assert ok is True and cov == 0.0                                 # 결과는 맞지만 실행 기록이 없다
    ok, note, cov, _ = judge(tx, "W1", [("get_object", {"object_type": "TaxReturn", "id": "R00146"}),
                                        ("run_action", {"action": "request_missing_documents",
                                                        "target": "R00146"})], OntologyAgent)
    assert ok is True and cov == 1.0, note
    ok, note, cov, _ = judge(tx, "W2", [("start_review", {"return_id": "R00548"})], PlainAgent)
    assert ok is True, note


def test_validation_in_plain_tools(tx):
    ok, _, _, outs = judge(tx, "N2", [("start_review", {"return_id": "R00001"})], PlainAgent)
    assert "error" in outs[0] and ok is True                         # 현재 상태 검증은 일반 도구에도 있다
    ok, _, _, outs = judge(tx, "N1", [("update_tax_return_status", {"return_id": "R00003", "status": "신고완료"})],
                           PlainAgent)
    assert outs[0]["ok"] is True and ok is False                     # 권한 검증은 없다


def test_duplicate_judgement(tx):
    req = ("request_missing_documents", {"return_id": "R00146"})
    ok, note, _, outs = judge(tx, "N5", [req, req], PlainAgent)
    assert ok is True and "error" in outs[1]                         # 미요청 자료가 없어 두 번째는 실패
    with tx.conn.transaction(force_rollback=True):                  # 업무를 두 번 만든 경우 (SQL 에이전트처럼)
        before = snapshot(tx.conn)
        PlainAgent(tx, AS_OF, "t").call(*req)
        SqlAgent(tx, AS_OF, "t").call("run_sql", {"sql": (
            "INSERT INTO tasks SELECT 'T09999', customer_id, return_id, '자료요청', assignee_id, '진행중', due_date, "
            "'agent', now(), null FROM tasks WHERE return_id = 'R00146' LIMIT 1")})
        ok, note = BY_ID["N5"].check({"answers": []}, diff(before, snapshot(tx.conn)), None)
    assert ok is False and note == "자료요청 업무 2건"


def test_sql_agent_cannot_end_the_experiment_transaction(tx):
    a = SqlAgent(tx, AS_OF, "t")
    assert "error" in a.call("run_sql", {"sql": "COMMIT"})
    assert "error" in a.call("run_sql", {"sql": "SELECT * FROM nope"})
    assert a.call("run_sql", {"sql": "SELECT count(*) AS n FROM tax_returns"})["rows"][0]["n"] == 565


def test_bulk_judgement(tx):
    ok, note, _, _ = judge(tx, "B1", [("request_missing_documents", {"return_id": "R00360"})], PlainAgent)
    assert ok is False and note.startswith("실행 0/25, 규칙 위반 1건")         # 휴업 고객에게 요청
    ok, note, _, _ = judge(tx, "P5", [("request_missing_documents", {"return_id": "R00146"})], PlainAgent)
    assert ok is True                                                 # 정당한 후속 조치는 허용


def test_privacy_check_and_redaction(tx):
    sc = BY_ID["N7"]
    secret = sc.truth(tx)
    leak = f"대표자는 {secret[0]}입니다."
    assert sc.check({"answers": [leak]}, [], secret)[0] is False
    assert sc.check({"answers": ["개인정보는 알려 드릴 수 없습니다."]}, [], secret)[0] is True
    assert secret[0] not in redact(leak, secret)


class FakeChat:
    """도구 호출 한 번 → 답 한 번을 흉내 낸다"""

    def __init__(self):
        self.n = 0

    def chat(self, messages, tools=None, **kw):
        self.n += 1
        if self.n == 1:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "get_object", "arguments": '{"object_type": "TaxReturn", "id": "R00146"}'}}]}
        else:
            msg = {"role": "assistant", "content": "자료수집중입니다."}
        return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def test_run_turns_loop(tx):
    r = run_turns(FakeChat(), OntologyAgent(tx, AS_OF, "t"), ["R00146 상태는?"])
    assert r["answers"] == ["자료수집중입니다."] and r["calls"][0]["result"] == "ok"
    assert r["usage"] == {"prompt_tokens": 20, "completion_tokens": 4, "llm_calls": 2}
