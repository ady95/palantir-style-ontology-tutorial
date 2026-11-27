"""Outbox Dispatcher — 아웃박스의 이벤트를 외부 시스템으로 보낸다 (23장)

Action 은 '보낼 것'을 변경과 같은 트랜잭션에 outbox 로 남기기만 한다(18-3). 이 작업자가 outbox 를 읽어
외부로 보내고, 결과를 다시 온톨로지에 기록한다.

    dispatcher = OutboxDispatcher(client, handlers={"documents_requested": notify_customer},
                                  on_delivered=record, on_dead=report)
    dispatcher.run_until_idle()

보장하는 것
- 적어도 한 번(at-least-once): 보낸 뒤 '보냈음'을 커밋하기 전에 죽으면 다시 보낸다. 그래서 외부 호출에는
  이벤트 ID 로 만든 멱등 키(outbox-<event_id>)를 붙이고, 외부 시스템이 중복을 걸러야 한다.
- 대상별 순서: 같은 객체(object_type, target_pk)의 이벤트는 앞의 것이 끝나야(published·dead) 다음 것을 보낸다.
- 여러 작업자: FOR UPDATE SKIP LOCKED 로 다른 작업자가 잡은 이벤트는 건너뛴다.
- 재시도와 포기: 일시적 실패는 지수 백오프로 다시, max_attempts 를 넘거나 영구 실패면 dead (사람이 처리).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .actions import ENGINE_SCHEMA

if TYPE_CHECKING:
    from .objects import OntologyClient


class DeliveryError(Exception):
    """외부 발송 실패. retryable=False 면 다시 보내도 소용없다 (예: 수신 거부 번호)"""
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class SimulatedCrash(Exception):
    """실습용: 외부 발송은 성공했지만 '보냈음'을 기록하기 전에 작업자가 죽은 상황"""


@dataclass
class Delivery:
    """외부 발송 결과"""
    channel: str
    recipient: str
    external_id: str
    duplicate: bool = False             # 외부 시스템이 같은 멱등 키를 이미 받은 경우
    detail: str | None = None


@dataclass
class Outcome:
    event_id: int
    event_type: str
    target: str
    result: str                         # published | retry | dead
    attempts: int
    detail: str = ""
    delivery: Delivery | None = None
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (f"#{self.event_id:<4d} {self.event_type:28s} {self.target:7s} → {self.result:9s} "
                f"(시도 {self.attempts}) {self.detail}")


Handler = Callable[[dict, "OntologyClient"], Delivery]


class OutboxDispatcher:
    def __init__(self, client: OntologyClient, handlers: dict[str, Handler], *, max_attempts: int = 5,
                 backoff: float = 1.0, batch: int = 20,
                 on_delivered: Callable[[dict, Delivery], Any] | None = None,
                 on_dead: Callable[[dict, str], Any] | None = None):
        self.client, self.handlers = client, handlers
        self.max_attempts, self.backoff, self.batch = max_attempts, backoff, batch
        self.on_delivered, self.on_dead = on_delivered, on_dead
        self.crash_after_send = False                    # 실습용 장애 주입

    @property
    def conn(self):
        return self.client.conn

    @staticmethod
    def idempotency_key(event: dict) -> str:
        return f"outbox-{event['event_id']}"

    # ------------------------------------------------------------------ 한 번 돌기
    def run_once(self) -> list[Outcome]:
        """보낼 차례인 이벤트를 한 묶음 처리한다. 묶음 전체가 한 트랜잭션이다."""
        self.client.actions.ensure_tables()
        types = list(self.handlers)
        outcomes: list[Outcome] = []
        with self.conn.transaction():
            events = self.conn.execute(
                f"""SELECT o.* FROM {ENGINE_SCHEMA}.outbox o
                    WHERE o.status = 'pending' AND o.event_type = ANY(%s) AND o.next_attempt_at <= clock_timestamp()
                      AND NOT EXISTS (                       -- 같은 대상의 앞선 이벤트가 아직 끝나지 않았으면 기다린다
                          SELECT 1 FROM {ENGINE_SCHEMA}.outbox p
                          WHERE p.object_type = o.object_type AND p.target_pk = o.target_pk
                            AND p.event_id < o.event_id AND p.status = 'pending' AND p.event_type = ANY(%s))
                    ORDER BY o.event_id LIMIT %s
                    FOR UPDATE SKIP LOCKED""", (types, types, self.batch)).fetchall()
            for ev in events:
                outcomes.append(self._deliver(ev))
        return outcomes

    def run_until_idle(self, timeout: float = 30.0, poll: float = 0.1) -> list[Outcome]:
        """보낼 것이 없어질 때까지 (재시도 대기 중인 이벤트는 시각이 될 때까지 기다리며) 반복한다"""
        import time
        deadline, outcomes = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            done = self.run_once()
            outcomes += done
            if not done and not self.pending():
                break
            if not done:
                time.sleep(poll)
        return outcomes

    def pending(self) -> int:
        return self.conn.execute(
            f"SELECT count(*) AS n FROM {ENGINE_SCHEMA}.outbox WHERE status = 'pending' AND event_type = ANY(%s)",
            (list(self.handlers),)).fetchone()["n"]

    # ------------------------------------------------------------------ 이벤트 하나
    def _deliver(self, ev: dict) -> Outcome:
        attempts = ev["attempts"] + 1
        out = Outcome(ev["event_id"], ev["event_type"], ev["target_pk"], "published", attempts)
        try:
            delivery = self.handlers[ev["event_type"]](ev, self.client)       # 외부 호출 (롤백 불가)
        except DeliveryError as e:
            return self._fail(ev, out, str(e), e.retryable)
        except Exception as e:  # noqa: BLE001 — 예상하지 못한 오류(네트워크 등)는 일시적인 것으로 보고 다시 시도
            return self._fail(ev, out, f"{type(e).__name__}: {e}", True)
        if self.crash_after_send:
            raise SimulatedCrash(f"#{ev['event_id']} 발송 직후 작업자 중단 (기록 전)")
        out.delivery = delivery
        out.detail = f"{delivery.channel}→{delivery.recipient} {delivery.external_id}" + \
            (" (외부에서 중복으로 걸러짐)" if delivery.duplicate else "")
        note = None
        if self.on_delivered:
            with self.conn.transaction():                 # 결과 기록(Action)이 실패해도 발송 사실은 남긴다
                note = self.on_delivered(ev, delivery)
        self.conn.execute(
            f"""UPDATE {ENGINE_SCHEMA}.outbox SET status = 'published', published_at = clock_timestamp(),
                attempts = %s, last_error = %s WHERE event_id = %s""", (attempts, note, ev["event_id"]))
        return out

    def _fail(self, ev: dict, out: Outcome, error: str, retryable: bool) -> Outcome:
        out.detail = error
        if retryable and out.attempts < self.max_attempts:
            delay = self.backoff * 2 ** (out.attempts - 1)
            self.conn.execute(
                f"""UPDATE {ENGINE_SCHEMA}.outbox SET attempts = %s, last_error = %s,
                    next_attempt_at = clock_timestamp() + make_interval(secs => %s) WHERE event_id = %s""",
                (out.attempts, error, delay, ev["event_id"]))
            out.result, out.extra["next_in"] = "retry", delay
            out.detail += f" — {delay:g}초 뒤 다시"
            return out
        self.conn.execute(
            f"""UPDATE {ENGINE_SCHEMA}.outbox SET status = 'dead', attempts = %s, last_error = %s
                WHERE event_id = %s""", (out.attempts, error, ev["event_id"]))
        out.result = "dead"
        out.detail += " — 포기(사람이 처리)" if retryable else " — 다시 보내도 안 됨(사람이 처리)"
        if self.on_dead:
            with self.conn.transaction():
                self.on_dead(ev, error)
        return out


# ====================================================================== 운영 도구
def outbox_rows(client: OntologyClient, status: str | None = None, limit: int = 50) -> list[dict]:
    client.actions.ensure_tables()
    q = f"""SELECT event_id, event_type, object_type, target_pk, status, attempts, next_attempt_at,
                   published_at, last_error FROM {ENGINE_SCHEMA}.outbox"""
    if status:
        return client.conn.execute(q + " WHERE status = %s ORDER BY event_id LIMIT %s", (status, limit)).fetchall()
    return client.conn.execute(q + " ORDER BY event_id LIMIT %s", (limit,)).fetchall()


def requeue(client: OntologyClient, event_id: int) -> bool:
    """dead 이벤트를 다시 보낼 차례로 되돌린다 (원인을 고친 뒤 사람이 실행)"""
    client.actions.ensure_tables()
    cur = client.conn.execute(
        f"""UPDATE {ENGINE_SCHEMA}.outbox SET status = 'pending', attempts = 0, next_attempt_at = clock_timestamp()
            WHERE event_id = %s AND status = 'dead'""", (event_id,))
    return cur.rowcount == 1
