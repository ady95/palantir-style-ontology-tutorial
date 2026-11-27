"""이벤트 구독 — 이벤트가 오면 후속 Action 을 실행한다 (24장)

정의 파일의 subscriptions 섹션:

    subscriptions:
      auto_start_review:
        description: 자료가 모두 접수되면 신고서 검토를 시작한다
        event: document_received        # 자료 접수 Action 이 내는 이벤트 (대상: DocumentSubmission)
        target: tax_return              # 이벤트 대상에서 Action 대상까지의 링크 경로
        action: start_review

    runner = SubscriptionRunner(client)
    runner.run_once()                    # 처리하지 않은 이벤트를 구독마다 처리
    runner.replay("auto_start_review")   # 처음부터 다시 처리 (멱등 키 덕분에 같은 실행은 한 번)

- 구독자마다 어느 이벤트를 처리했는지 minionto.event_deliveries 에 따로 남긴다. outbox 의 status 는
  23장 발송 작업자의 것이므로 건드리지 않는다. 같은 이벤트를 여러 구독자가 각자 처리할 수 있다.
- 실행 전에 check()(20장)로 판단한다. 실행할 수 없으면(예: 아직 미제출 자료가 있음) 실행 기록을 남기지 않고
  skipped 로만 기록한다. 판단과 실행 사이에 상황이 바뀌어도 apply() 가 다시 검사한다.
- Action 은 시스템 실행자로, 멱등 키 'sub:<구독>:<이벤트 번호>' 를 붙여 실행한다(22장). 재처리해도 같은
  이벤트로 같은 Action 이 두 번 실행되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .actions import ENGINE_SCHEMA as S

if TYPE_CHECKING:
    from .objects import OntologyClient


@dataclass
class Handled:
    subscriber: str
    event_id: int
    event_type: str
    target: str
    status: str             # done | skipped
    result: str             # Action 결과 또는 check() 판단
    detail: str = ""

    def __str__(self) -> str:
        return (f"{self.subscriber:18s} #{self.event_id:<4d} {self.event_type:20s} → {self.target:7s} "
                f"{self.status:7s} {self.result}" + (f" — {self.detail}" if self.detail else ""))


class SubscriptionRunner:
    def __init__(self, client: OntologyClient, batch: int = 50):
        self.client, self.batch = client, batch
        self.subs = client.onto.subscriptions

    def actor(self, name: str) -> dict:
        return {"id": f"subscription:{name}", "role": "시스템", "type": "system"}

    def run_once(self) -> list[Handled]:
        self.client.actions.ensure_tables()
        out: list[Handled] = []
        for name, sub in self.subs.items():
            events = self.client.conn.execute(
                f"""SELECT o.* FROM {S}.outbox o
                    WHERE o.event_type = %s AND NOT EXISTS (
                        SELECT 1 FROM {S}.event_deliveries d WHERE d.subscriber = %s AND d.event_id = o.event_id)
                    ORDER BY o.event_id LIMIT %s""", (sub.event, name, self.batch)).fetchall()
            for ev in events:
                try:
                    h = self._handle(name, sub, ev)
                except _Retry:                                      # 일시 실패: 처리 기록이 롤백되어 다음에 다시
                    continue
                if h:
                    out.append(h)
        return out

    def _handle(self, name: str, sub, ev: dict) -> Handled | None:
        conn, a = self.client.conn, self.client.actions
        with conn.transaction():
            claimed = conn.execute(                                 # 다른 실행기와 겹치면 먼저 잡은 쪽이 처리
                f"""INSERT INTO {S}.event_deliveries (subscriber, event_id, status) VALUES (%s, %s, 'skipped')
                    ON CONFLICT DO NOTHING RETURNING event_id""", (name, ev["event_id"])).fetchone()
            if not claimed:
                return None
            target = self._target(sub, ev)
            h = Handled(name, ev["event_id"], ev["event_type"], str(target), "skipped", "")
            if target is None:
                h.target, h.result, h.detail = "-", "no_target", "링크로 대상을 찾지 못했습니다"
            else:
                chk = a.check(sub.action, target, actor=self.actor(name), params=sub.params)
                if chk.ok:
                    res = a.apply(sub.action, target, actor=self.actor(name), params=sub.params,
                                  idempotency_key=f"sub:{name}:{ev['event_id']}")
                    if res.status == "failed" and res.retryable:
                        raise _Retry()                              # 기록을 롤백해 다음에 다시 처리
                    h.status, h.result = "done", res.status + (" (replayed)" if res.replayed else "")
                    h.detail = res.messages[0] if res.messages else ""
                    conn.execute(f"UPDATE {S}.event_deliveries SET execution_id = %s WHERE subscriber = %s "
                                 "AND event_id = %s", (res.execution_id, name, ev["event_id"]))
                else:
                    h.result, h.detail = chk.status, chk.messages[0] if chk.messages else ""
            conn.execute(f"""UPDATE {S}.event_deliveries SET status = %s, result = %s, detail = %s
                             WHERE subscriber = %s AND event_id = %s""",
                         (h.status, h.result, h.detail, name, ev["event_id"]))
            return h

    def _target(self, sub, ev: dict):
        if sub.target == "$event.target":
            return ev["target_pk"]
        obj = self.client.get(ev["object_type"], ev["target_pk"])
        if obj is None:
            return None
        for step in sub.target.split("."):
            nxt = getattr(obj, step)
            obj = nxt.first() if hasattr(nxt, "first") else nxt
            if obj is None:
                return None
        return obj.pk

    def replay(self, subscriber: str, from_event_id: int = 0) -> int:
        """구독자의 처리 기록을 지워 다시 처리하게 한다. 이미 실행한 Action 은 멱등 키로 재생된다."""
        self.client.actions.ensure_tables()
        cur = self.client.conn.execute(
            f"DELETE FROM {S}.event_deliveries WHERE subscriber = %s AND event_id >= %s", (subscriber, from_event_id))
        return cur.rowcount


class _Retry(Exception):
    pass

