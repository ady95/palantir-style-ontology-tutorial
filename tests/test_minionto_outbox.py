"""23장. 아웃박스 발송 작업자 · 외부 연동 · 결과 기록 테스트

외부 발송 서비스는 같은 프로세스의 모의 서비스(examples/mock_notifier.py)를 쓴다.
대부분 바깥 트랜잭션 안에서 실행하고 롤백한다. 작업자 두 개의 동시 실행만 두 연결이 필요해
이벤트를 실제로 커밋했다가 끝나면 지운다.
실행: uv run pytest -q tests/test_minionto_outbox.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "examples"))

from fastapi.testclient import TestClient  # noqa: E402
from mock_notifier import create_mock_notifier  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.outbox import OutboxDispatcher, SimulatedCrash, requeue  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}

spec = importlib.util.spec_from_file_location("taxos_integrations", TAXOS.parent / "integrations.py")
integrations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrations)


def _open():
    c = OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    return c


@pytest.fixture
def env():
    c = _open()
    app = create_mock_notifier()
    with c.conn.transaction(force_rollback=True):
        notifier = integrations.Notifier(TestClient(app))
        d = integrations.make_dispatcher(c, notifier, backoff=0.05)
        yield c, d, app.state.box
    c.close()


def scalar(c, q, *args):
    return next(iter(c.conn.execute(q, args).fetchone().values()))


def low_risk(c, n=0):
    return [r.pk for r in c.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").limit(n + 1)][n]


def test_delivery_is_recorded_as_object(env):
    c, d, box = env
    ret = low_risk(c)
    assert c.actions.apply(REQ, ret, actor=AI).ok
    out = d.run_until_idle()
    assert [o.result for o in out] == ["published"]
    msg = list(box["messages"].values())[0]
    assert msg["channel"] == "문자" and "신고 자료를 요청드립니다" in msg["text"] and "****" in msg["to"]
    n = c.objects("Notification").where(return_id=ret).one()
    assert (n.status, n.recipient, n.external_id, n.attempts) == ("발송완료", "고객", "MSG-0001", 1)
    assert scalar(c, "SELECT status FROM minionto.outbox WHERE event_id = %s", out[0].event_id) == "published"
    assert n.event_id == out[0].event_id


def test_transient_failures_are_retried(env):
    c, d, box = env
    box["fail_next"] = 2
    c.actions.apply(REQ, low_risk(c), actor=AI)
    out = d.run_until_idle()
    assert [o.result for o in out] == ["retry", "retry", "published"]
    assert c.objects("Notification").where(event_type="documents_requested").one().attempts == 3


def test_permanent_failure_goes_dead_and_creates_task(env):
    c, d, box = env
    ret = low_risk(c)
    box["reject"].add(c.get("TaxReturn", ret).customer.phone)
    c.actions.apply(REQ, ret, actor=AI)
    out = d.run_until_idle()
    assert [o.result for o in out] == ["dead"] and out[0].attempts == 1
    n = c.objects("Notification").where(return_id=ret).one()
    assert n.status == "발송실패" and "수신 거부" in n.detail
    task = c.objects("Task").where(return_id=ret, task_type="고객연락").one()
    assert task.status == "대기" and task.created_by == "system"
    assert scalar(c, "SELECT count(*) FROM minionto.outbox WHERE event_type = 'notification_failed'") == 1
    # 원인을 고친 뒤 사람이 다시 보낼 차례로 되돌린다
    box["reject"].clear()
    assert requeue(c, out[0].event_id)
    assert [o.result for o in d.run_until_idle()] == ["published"]


def test_gives_up_after_max_attempts(env):
    c, _, box = env
    d = integrations.make_dispatcher(c, integrations.Notifier(TestClient(create_mock_notifier(fail_next=99))),
                                     backoff=0.02, max_attempts=3)
    c.actions.apply(REQ, low_risk(c), actor=AI)
    assert [o.result for o in d.run_until_idle()] == ["retry", "retry", "dead"]


def test_events_of_same_target_keep_order(env):
    c, d, box = env
    ret = low_risk(c)
    c.actions.apply(REQ, ret, actor=AI)
    c.actions.apply("cancel_document_request", ret, actor=TAX, params={"reason": "잘못 보냄"})
    box["fail_next"] = 1
    first = d.run_once()                                       # 요청 발송 실패 → 취소는 기다린다
    assert [(o.event_type, o.result) for o in first] == [("documents_requested", "retry")]
    d.run_until_idle()
    texts = [m["text"] for m in box["messages"].values()]
    assert "요청드립니다" in texts[0] and "취소합니다" in texts[1]


def test_crash_after_send_is_deduplicated(env):
    c, d, box = env
    c.actions.apply(REQ, low_risk(c), actor=AI)
    d.crash_after_send = True
    with pytest.raises(SimulatedCrash):
        d.run_once()
    assert len(box["messages"]) == 1                            # 외부에는 이미 나갔다
    assert scalar(c, "SELECT status FROM minionto.outbox WHERE event_type = 'documents_requested'"
                  ) == "pending"                                 # '보냈음' 기록은 롤백됨
    d.crash_after_send = False
    out = d.run_until_idle()
    assert out[0].result == "published" and out[0].delivery.duplicate
    assert len(box["messages"]) == 1 and box["calls"] == 2       # 두 번 불렀지만 메시지는 하나
    assert c.objects("Notification").count() == 1


def test_two_workers_do_not_take_the_same_event():
    """SKIP LOCKED: 한 작업자가 잡고 있는 이벤트는 다른 작업자가 건너뛴다 (이벤트를 커밋했다가 지운다)"""
    c1, c2 = _open(), _open()
    c1.actions.ensure_tables()
    ids = [r["event_id"] for r in c1.conn.execute(
        """INSERT INTO minionto.outbox (event_type, execution_id, object_type, target_pk, payload)
           SELECT 'approval_requested', gen_random_uuid(), 'TaxReturn', t, %s FROM unnest(%s::text[]) t
           RETURNING event_id""",
        (json.dumps({"action": REQ, "requested_by": AI, "changes": 1, "expires_at": None}),
         ["R00366", "R00517"])).fetchall()]
    app = create_mock_notifier()
    slow = integrations.handlers(integrations.Notifier(TestClient(app)))["approval_requested"]

    def slow_handler(ev, client):
        time.sleep(0.6)
        return slow(ev, client)
    got = {}
    try:
        w1 = OutboxDispatcher(c1, {"approval_requested": slow_handler}, batch=1)
        w2 = OutboxDispatcher(c2, integrations.handlers(integrations.Notifier(TestClient(app))), batch=5)
        th = threading.Thread(target=lambda: got.update(w1=w1.run_once()))
        th.start()
        time.sleep(0.2)                                          # w1 이 첫 이벤트를 잡고 발송 중
        got["w2"] = w2.run_once()
        th.join(10)
        assert [o.event_id for o in got["w1"]] == [ids[0]]
        assert [o.event_id for o in got["w2"]] == [ids[1]]
        assert len(app.state.box["messages"]) == 2
    finally:
        c1.conn.execute("DELETE FROM minionto.outbox WHERE event_id = ANY(%s)", (ids,))
        c1.close()
        c2.close()
