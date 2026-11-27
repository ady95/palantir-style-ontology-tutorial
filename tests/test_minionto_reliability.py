"""22장. 트랜잭션 경계 · 멱등성 · 오류 분류와 복구 테스트

바깥 트랜잭션 안에서 실행하고 롤백한다. 잠금 시간 초과와 같은 키의 동시 요청은 두 번째 연결로
실제 잠금을 잡아 재현하며, 그 연결도 커밋하지 않는다.
실행: uv run pytest -q tests/test_minionto_reliability.py
"""

from __future__ import annotations

import copy
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from fastapi.testclient import TestClient  # noqa: E402

from minionto import Ontology, OntologyClient  # noqa: E402
from minionto.api import Served, create_app  # noqa: E402
from minionto.auth import JwtAuth  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
RAW = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
TAX = {"id": "A001", "role": "세무사", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def _open(onto=None):
    c = OntologyClient(onto, base_dir=TAXOS.parent, today=AS_OF) if onto else OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    return c


@pytest.fixture
def tx():
    c = _open()
    with c.conn.transaction(force_rollback=True):
        yield c
    c.close()


def scalar(c, q, *args):
    return next(iter(c.conn.execute(q, args).fetchone().values()))


def target(c, high_risk=False):
    op = "eq" if high_risk else "ne"
    return c.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": op, "value": "높음"}).order_by("return_id").first().pk


# ================================================================ 멱등 키
def test_same_key_runs_once_and_replays(tx):
    ret = target(tx)
    tasks, logs = scalar(tx, "SELECT count(*) FROM tasks"), scalar(tx, "SELECT count(*) FROM minionto.action_log")
    first = tx.actions.apply(REQ, ret, actor=AI, idempotency_key="k-1")
    second = tx.actions.apply(REQ, ret, actor=AI, idempotency_key="k-1")
    assert first.ok and not first.replayed
    assert second.ok and second.replayed and second.execution_id == first.execution_id
    assert second.changes == first.to_dict()["changes"]
    assert scalar(tx, "SELECT count(*) FROM tasks") == tasks + 1                     # 한 번만 실행
    assert scalar(tx, "SELECT count(*) FROM minionto.action_log") == logs + 1        # 재생은 실행 기록 아님
    assert scalar(tx, "SELECT replays FROM minionto.idempotency_keys WHERE key = 'k-1'") == 1
    # 키 없이 다시 보내면 업무 규칙(중복 방지)이 막는다 — 실행은 되지만 rejected
    assert tx.actions.apply(REQ, ret, actor=AI).status == "rejected"


def test_key_mismatch_and_scope(tx):
    ret = target(tx)
    assert tx.actions.apply(REQ, ret, actor=AI, idempotency_key="k-2").ok
    other = tx.actions.apply(REQ, "R00012", actor=AI, idempotency_key="k-2")
    assert other.status == "invalid" and "이미 다른 요청" in other.messages[0]
    stored = scalar(tx, "SELECT response->>'status' FROM minionto.idempotency_keys WHERE key = 'k-2'")
    assert stored == "completed"                                                     # 원래 응답은 그대로
    # 키는 실행자마다 따로 — 세무사가 같은 키를 쓰면 별개의 요청
    again = tx.actions.apply(REQ, ret, actor=TAX, idempotency_key="k-2")
    assert not again.replayed and again.status == "rejected"


def test_failure_and_dry_run_do_not_consume_key():
    raw = copy.deepcopy(RAW)
    raw["object_types"]["DocumentSubmission"].pop("state_machine")             # DB 제약을 일부러 어기려고
    raw["actions"]["broken_submit"] = {
        "display_name": "잘못 정의된 제출 처리", "target": "TaxReturn", "permission": {"roles": ["세무사"]},
        "edits": [{"update_linked": "submissions", "set": {"status": "제출완료", "submitted_at": None}}]}
    c = _open(Ontology.model_validate(raw))
    try:
        with c.conn.transaction(force_rollback=True):
            ret = c.objects("TaxReturn").where(status="자료수집중").order_by("return_id").first()
            res = c.actions.apply("broken_submit", ret, actor=TAX, idempotency_key="k-3", retries=3)
            assert res.status == "failed" and not res.retryable and res.error_code == "23514"
            assert res.attempts == 1                                                 # 영구 오류는 재시도 안 함
            assert scalar(c, "SELECT count(*) FROM minionto.idempotency_keys WHERE key = 'k-3'") == 0
            assert scalar(c, "SELECT error->>'code' FROM minionto.action_log WHERE execution_id = %s",
                          res.execution_id) == "23514"
            c.actions.apply(REQ, ret, actor=TAX, idempotency_key="k-4", dry_run=True)
            assert scalar(c, "SELECT count(*) FROM minionto.idempotency_keys WHERE key = 'k-4'") == 0
    finally:
        c.close()


def test_concurrent_same_key_waits_for_first():
    """같은 키의 두 번째 요청은 첫 트랜잭션이 끝날 때까지 기다린다. 첫 요청이 롤백되면 두 번째가 실행된다"""
    c1, c2 = _open(), _open()
    out = {}
    try:
        with c1.conn.transaction(force_rollback=True):
            ret = target(c1)
            assert c1.actions.apply(REQ, ret, actor=AI, idempotency_key="k-5").ok   # 커밋 전

            def second():
                with c2.conn.transaction(force_rollback=True):
                    t0 = time.monotonic()
                    out["res"] = c2.actions.apply(REQ, ret, actor=AI, idempotency_key="k-5")
                    out["waited"] = time.monotonic() - t0
            th = threading.Thread(target=second)
            th.start()
            time.sleep(0.8)
        th.join(10)                                                                   # c1 롤백 후 진행
        assert out["waited"] >= 0.7
        assert out["res"].ok and not out["res"].replayed
    finally:
        c1.close()
        c2.close()


# ================================================================ 업무 규칙에 의한 중복 방지
def test_pending_approval_is_reused(tx):
    ret = target(tx, high_risk=True)
    a = tx.actions.apply(REQ, ret, actor=AI)
    b = tx.actions.apply(REQ, ret, actor=AI)
    assert a.status == b.status == "approval_required" and a.approval_id == b.approval_id
    assert any("이미 대기 중" in m for m in b.messages) and b.events == []
    assert scalar(tx, "SELECT count(*) FROM minionto.approval_requests WHERE target_pk = %s "
                      "AND status = 'pending'", ret) == 1
    tx.conn.execute("UPDATE minionto.approval_requests SET expires_at = expires_at - interval '25 hours' "
                    "WHERE approval_id = %s", (a.approval_id,))
    assert tx.actions.apply(REQ, ret, actor=AI).approval_id != a.approval_id       # 기한 지난 요청은 재사용 안 함


# ================================================================ 오류 분류와 재시도
def hold_lock(c2, ret):
    c2.conn.execute("BEGIN")
    c2.conn.execute("SELECT 1 FROM tax_returns WHERE return_id = %s FOR UPDATE", (ret,))


def test_lock_timeout_is_retryable_and_retried():
    c1, c2 = _open(), _open()
    c1.actions.lock_timeout, c1.actions.retry_backoff = "300ms", 0.4
    try:
        with c1.conn.transaction(force_rollback=True):
            ret = target(c1)
            hold_lock(c2, ret)
            res = c1.actions.apply(REQ, ret, actor=AI)
            assert res.status == "failed" and res.retryable and res.error_code == "55P03"
            threading.Timer(0.45, lambda: c2.conn.execute("ROLLBACK")).start()     # 잠시 뒤 잠금을 푼다
            hold = c1.actions.apply(REQ, ret, actor=AI, retries=3)
            assert hold.ok and hold.attempts == 2
            assert scalar(c1, "SELECT count(*) FROM minionto.action_log WHERE target_pk = %s "
                              "AND status = 'failed'", ret) == 2                        # 실패도 시도마다 기록
    finally:
        c2.conn.execute("ROLLBACK")
        c1.close()
        c2.close()


def test_api_idempotency_header_and_503():
    client, c2 = _open(), _open()
    auth = JwtAuth("test-only-secret-0123456789abcdef-0123456789")
    served = Served.from_file(TAXOS)
    client.actions.lock_timeout = "200ms"
    tok = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=["objects:read", "actions:apply"])
    h = {"Authorization": f"Bearer {tok}"}
    try:
        with client.conn.transaction(force_rollback=True):
            http = TestClient(create_app([served], auth=auth, today=AS_OF, connect=lambda s: nullcontext(client)))
            ret = target(client)
            hold_lock(c2, ret)
            busy = http.post("/v1/ontologies/taxos/actions/request_missing_documents/apply", json={"target": ret},
                             headers={**h, "Idempotency-Key": "api-1"})
            assert busy.status_code == 503 and busy.headers["Retry-After"] == "1"
            c2.conn.execute("ROLLBACK")
            ok = http.post("/v1/ontologies/taxos/actions/request_missing_documents/apply", json={"target": ret},
                           headers={**h, "Idempotency-Key": "api-1"})                 # 실패는 키를 쓰지 않았다
            again = http.post("/v1/ontologies/taxos/actions/request_missing_documents/apply", json={"target": ret},
                              headers={**h, "Idempotency-Key": "api-1"})
            assert ok.status_code == 200 and "Idempotent-Replayed" not in ok.headers
            assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
            assert again.json()["execution_id"] == ok.json()["execution_id"]
    finally:
        c2.conn.execute("ROLLBACK")
        client.close()
        c2.close()


# ================================================================ 보상 Action
def test_compensating_action(tx):
    ret = target(tx)
    done = tx.actions.apply(REQ, ret, actor=AI)
    requested = [c["pk"] for c in done.changes if c["object_type"] == "DocumentSubmission"]
    assert tx.actions.apply("cancel_document_request", ret, actor=AI, params={"reason": "x"}).status == "denied"
    undo = tx.actions.apply("cancel_document_request", ret, actor=TAX, params={"reason": "다른 신고에 보낼 요청이었음"})
    assert undo.ok and undo.events == ["documents_request_cancelled"]
    for pk in requested:
        s = tx.get("DocumentSubmission", pk)
        assert s.status == "미요청" and s.requested_at is None
    task = [c["pk"] for c in done.changes if c["object_type"] == "Task"][0]
    assert tx.get("Task", task).status == "취소"
    assert ("DocumentSubmission", "요청됨", "미요청") in {(t["object_type"], t["from"], t["to"]) for t in undo.transitions}
    assert tx.actions.apply("cancel_document_request", ret, actor=TAX, params={"reason": "x"}).status == "rejected"
