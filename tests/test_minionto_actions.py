"""18장. Function Engine · Action Engine 테스트

Action 은 실제로 DB를 바꾸므로, 각 테스트를 바깥 트랜잭션 안에서 실행하고 끝나면 롤백한다.
(엔진 안의 트랜잭션은 SAVEPOINT 가 된다.) DB에 연결할 수 없으면 건너뛴다.
실행: uv run pytest -q tests/test_minionto_actions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import Ontology, OntologyClient, QueryError  # noqa: E402
from minionto.functions import FunctionError  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"

TAX_ACCOUNTANT = {"id": "A001", "role": "세무사", "type": "human"}
AI_AGENT = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def _open(onto=None):
    c = OntologyClient(onto, base_dir=TAXOS.parent, today=AS_OF) if onto else \
        OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    return c


@pytest.fixture
def tx():
    c = _open()
    with c.conn.transaction(force_rollback=True):      # 테스트가 끝나면 모든 변경을 되돌린다
        yield c
    c.close()


def scalar(c, q, *args):
    return next(iter(c.conn.execute(q, args).fetchone().values()))


def target(c, high_risk: bool):
    op = "eq" if high_risk else "ne"
    return (c.actions.candidates(REQ, actor=AI_AGENT)
            .where({"field": "customer.risk_level", "op": op, "value": "높음"})
            .order_by("return_id").first())


# ================================================================ Function Engine
def test_functions_match_sql(tx):
    fn = tx.functions
    for acc in ("A002", "A004"):
        n, mx = tx.conn.execute(
            "SELECT count(*) n, max(a.max_workload) mx FROM customers c JOIN accountants a "
            "USING (accountant_id) WHERE accountant_id = %s", (acc,)).fetchone().values()
        assert fn.call("has_capacity", accountant=acc) == (n < mx)
    ret = tx.objects("TaxReturn").where(status="자료수집중").order_by("return_id").first()
    expected = [r["submission_id"] for r in tx.conn.execute(
        "SELECT submission_id FROM document_submissions WHERE return_id = %s "
        "AND status IN ('미요청','요청됨') ORDER BY doc_type_code", (ret.pk,)).fetchall()]
    assert [s.pk for s in fn.call("missing_documents", tax_return=ret)] == expected


def test_function_output_contract(tx):
    risk = tx.functions.call("filing_delay_risk", customer="C0008")
    assert set(risk) == {"score", "level", "reasons"} and 0 <= risk["score"] <= 100
    rec = tx.functions.call("recommend_assignee", customer="C0008")
    assert rec is None or rec.object_type == "Accountant"


def test_function_input_errors(tx):
    with pytest.raises(FunctionError, match="정의에 없는 입력"):
        tx.functions.call("has_capacity", accountant="A002", extra=1)
    with pytest.raises(FunctionError, match="필수 입력"):
        tx.functions.call("has_capacity")
    with pytest.raises(FunctionError, match="없습니다"):
        tx.functions.call("has_capacity", accountant="A999")
    with pytest.raises(FunctionError, match="정의되어 있지 않습니다"):
        tx.functions.call("no_such_function")


def test_function_wrong_return_type_is_caught(tx):
    tx.functions.implementation("has_capacity")                       # 모듈 로드
    mod = tx.functions._modules["functions"]
    original = mod.has_capacity
    mod.has_capacity = lambda ctx, accountant: None                   # 계약 위반 구현
    try:
        with pytest.raises(FunctionError, match="boolean"):
            tx.functions.call("has_capacity", accountant="A002")
    finally:
        mod.has_capacity = original


# ================================================================ Action Engine
def test_request_documents_end_to_end(tx):
    ret = target(tx, high_risk=False)
    open_before = scalar(tx, "SELECT count(*) FROM document_submissions WHERE return_id=%s AND status='미요청'", ret.pk)
    assert open_before > 0

    res = tx.actions.apply(REQ, ret, actor=AI_AGENT, params={"note": "테스트"})
    assert res.status == "completed", res.messages
    assert scalar(tx, "SELECT count(*) FROM document_submissions WHERE return_id=%s AND status='미요청'", ret.pk) == 0
    task = [c for c in res.changes if c["op"] == "create"][0]
    assert task["pk"].startswith("T") and task["after"]["created_by"] == "agent"
    assert scalar(tx, "SELECT task_type FROM tasks WHERE task_id=%s", task["pk"]) == "자료요청"
    assert scalar(tx, "SELECT count(*) FROM minionto.outbox WHERE execution_id=%s", res.execution_id) == 1
    assert scalar(tx, "SELECT status FROM minionto.action_log WHERE execution_id=%s", res.execution_id) == "completed"

    again = tx.actions.apply(REQ, ret, actor=AI_AGENT)               # 중복 실행 방지
    assert again.status == "rejected"
    assert any("중복 실행 방지" in m for m in again.messages)


def test_dry_run_changes_nothing(tx):
    ret = target(tx, high_risk=False)
    tasks = scalar(tx, "SELECT count(*) FROM tasks")
    res = tx.actions.apply(REQ, ret, actor=AI_AGENT, dry_run=True)
    assert res.status == "completed" and res.dry_run and res.changes
    assert scalar(tx, "SELECT count(*) FROM tasks") == tasks
    assert tx.actions.candidates(REQ, actor=AI_AGENT).where(return_id=ret.pk).exists()


def test_sequence_ids_are_consecutive(tx):
    a, b = target(tx, False), tx.actions.candidates(REQ, actor=AI_AGENT).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("-return_id").first()
    ids = [next(c["pk"] for c in tx.actions.apply(REQ, t, actor=AI_AGENT).changes if c["op"] == "create")
           for t in (a, b)]
    assert int(ids[1][1:]) == int(ids[0][1:]) + 1


def test_permission(tx):
    ret = target(tx, high_risk=False)
    owner = ret.customer.accountant_id
    other = next(a for a in ("A002", "A003", "A004") if a != owner)
    assert tx.actions.apply(REQ, ret, actor={"id": other, "role": "실무자", "type": "human"}).status == "denied"
    assert tx.actions.apply(REQ, ret, actor={"id": owner, "role": "실무자", "type": "human"}).status == "completed"
    # 역할 검사가 입력 검사보다 먼저: 권한 없는 실행자에게는 파라미터 오류도 알려주지 않는다
    res = tx.actions.apply("assign_accountant", "C0001", actor=AI_AGENT)
    assert res.status == "denied" and "AI비서" in res.messages[0]
    res = tx.actions.apply("assign_accountant", "C0001", actor={**AI_AGENT, "role": "세무사"})
    assert res.status == "denied" and "agent" in res.messages[0]          # 역할은 맞아도 human 전용
    # 객체를 파라미터로 넘긴 요청도 감사 로그에 남는다 (기본키로 기록)
    res = tx.actions.apply("assign_accountant", "C0001", actor=AI_AGENT,
                           params={"accountant": tx.get("Accountant", "A004")})
    assert scalar(tx, "SELECT params->>'accountant' FROM minionto.action_log WHERE execution_id=%s",
                  res.execution_id) == "A004"


def test_candidates_respect_permission(tx):
    staff = {"id": "A002", "role": "실무자", "type": "human"}
    mine = tx.actions.candidates(REQ, actor=staff)
    assert mine.count() > 0
    assert mine.count() == mine.where(**{"customer.accountant_id": "A002"}).count()
    assert tx.actions.candidates("assign_accountant", actor=AI_AGENT).count() == 0


def test_approval_flow(tx):
    ret = target(tx, high_risk=True)
    res = tx.actions.apply(REQ, ret, actor=AI_AGENT)
    assert res.status == "approval_required" and not res.changes
    assert scalar(tx, "SELECT count(*) FROM tasks WHERE return_id=%s AND task_type='자료요청' "
                      "AND status IN ('대기','진행중')", ret.pk) == 0
    aid = res.approval_id
    assert tx.actions.decide(aid, {"id": "A002", "role": "실무자", "type": "human"}).status == "denied"
    assert tx.actions.decide(aid, {**AI_AGENT, "role": "세무사", "type": "human"}).status == "denied"  # 본인
    done = tx.actions.decide(aid, TAX_ACCOUNTANT, comment="ok")
    assert done.status == "completed" and done.approval_id == aid
    assert scalar(tx, "SELECT status FROM minionto.approval_requests WHERE approval_id=%s", aid) == "approved"
    with pytest.raises(QueryError, match="approved"):
        tx.actions.decide(aid, TAX_ACCOUNTANT)


def test_approval_expires_when_situation_changes(tx):
    ret = target(tx, high_risk=True)
    aid = tx.actions.apply(REQ, ret, actor=AI_AGENT).approval_id
    assert tx.actions.apply(REQ, ret, actor=TAX_ACCOUNTANT).status == "completed"   # 사람이 먼저 처리
    late = tx.actions.decide(aid, TAX_ACCOUNTANT)
    assert late.status == "rejected"
    assert scalar(tx, "SELECT status FROM minionto.approval_requests WHERE approval_id=%s", aid) == "expired"


def test_function_precondition(tx):
    cust = tx.objects("Customer").where(accountant_id="A002").order_by("customer_id").first()
    full = tx.actions.apply("assign_accountant", cust, actor=TAX_ACCOUNTANT, params={"accountant": "A003"})
    assert full.status == "rejected" and "최대치" in full.messages[0]
    ok = tx.actions.apply("assign_accountant", cust, actor=TAX_ACCOUNTANT, params={"accountant": "A004"})
    assert ok.status == "completed"
    assert tx.get("Customer", cust.pk).accountant_id == "A004"


def test_invalid_inputs(tx):
    a = tx.actions
    assert a.apply("update_risk_level", "C0001", actor=TAX_ACCOUNTANT,
                   params={"level": "최고", "reason": "x"}).status == "invalid"
    assert a.apply("update_risk_level", "C0001", actor=TAX_ACCOUNTANT,
                   params={"level": "높음", "reason": "x", "extra": 1}).status == "invalid"
    assert a.apply("update_risk_level", "C9999", actor=TAX_ACCOUNTANT,
                   params={"level": "높음", "reason": "x"}).status == "invalid"
    assert a.apply("update_risk_level", "C0001", actor={"id": "x", "role": "세무사"},
                   params={"level": "높음", "reason": "x"}).status == "invalid"


def test_failure_rolls_back_all_edits(tx):
    """두 번째 edit 이 DB 제약(제출완료면 제출일 필수)을 어기면 첫 번째 edit 도 반영되지 않는다"""
    raw = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
    raw["actions"]["broken_submit"] = {
        "display_name": "잘못 정의된 제출 처리", "target": "TaxReturn",
        "permission": {"roles": ["세무사"]},
        "edits": [
            {"create": "Task", "values": {"customer_id": "$target.customer_id", "task_type": "고객연락",
                                          "assignee_id": "A001", "status": "대기", "due_date": "$today",
                                          "created_by": "$actor.type", "created_at": "$now"}},
            {"update_linked": "submissions", "set": {"status": "제출완료", "submitted_at": None}},
        ]}
    c = _open(Ontology.model_validate(raw))
    try:
        with c.conn.transaction(force_rollback=True):
            ret = c.objects("TaxReturn").where(
                {"some": {"link": "submissions", "where": {"field": "status", "op": "eq", "value": "미요청"}}}
            ).order_by("return_id").first()
            tasks = scalar(c, "SELECT count(*) FROM tasks")
            res = c.actions.apply("broken_submit", ret, actor=TAX_ACCOUNTANT)
            assert res.status == "failed" and "CheckViolation" in res.messages[0]
            assert scalar(c, "SELECT count(*) FROM tasks") == tasks             # 첫 edit 도 롤백
            assert scalar(c, "SELECT status FROM minionto.action_log WHERE execution_id=%s",
                          res.execution_id) == "failed"                         # 실패 기록은 남는다
    finally:
        c.close()


def test_log_times_follow_execution_order(tx):
    """한 트랜잭션 안에서 여러 번 실행해도 실행 기록의 시각이 실행 순서대로 남는다 (clock_timestamp)"""
    ids = [tx.actions.apply("update_risk_level", "C0001", actor=AI_AGENT,
                            params={"level": "높음", "reason": "x"}).execution_id for _ in range(3)]
    rows = tx.conn.execute("""SELECT execution_id::text AS id, created_at FROM minionto.action_log
                              WHERE execution_id = ANY(%s::uuid[]) ORDER BY created_at""", (ids,)).fetchall()
    assert [r["id"] for r in rows] == ids
    assert rows[0]["created_at"] < rows[1]["created_at"] < rows[2]["created_at"]
