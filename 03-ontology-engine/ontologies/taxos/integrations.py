"""세무 온톨로지의 외부 연동 (23장) — 아웃박스 이벤트를 알림 메시지로 바꿔 보내고, 결과를 Action 으로 기록한다

    notifier = Notifier(httpx.Client(base_url="https://발송서비스"))
    dispatcher = make_dispatcher(client, notifier)
    dispatcher.run_until_idle()

엔진(minionto/outbox.py)은 '언제, 몇 번, 어떤 순서로' 보낼지를 맡고, 이 파일은 '무엇을, 누구에게'만 정한다.
메시지에는 업무 용어(세목, 자료 이름, 기한)만 쓰고 테이블·컬럼 이름은 쓰지 않는다.
"""

from __future__ import annotations

from typing import Any

from minionto.outbox import Delivery, DeliveryError, OutboxDispatcher

SYSTEM = {"id": "outbox-dispatcher", "role": "시스템", "type": "system"}

# 이벤트 → (채널, 받는 쪽)
ROUTES = {
    "documents_requested": ("문자", "고객"),
    "documents_request_cancelled": ("문자", "고객"),
    "approval_requested": ("메신저", "세무사"),
}


class Notifier:
    """외부 발송 서비스 클라이언트. http 는 base_url 이 정해진 httpx.Client (실습에서는 모의 서비스)"""

    def __init__(self, http: Any):
        self.http = http

    def send(self, key: str, to: str, channel: str, text: str) -> dict:
        r = self.http.post("/v1/messages", json={"to": to, "channel": channel, "text": text},
                           headers={"Idempotency-Key": key})
        if r.status_code >= 500 or r.status_code == 429:
            raise DeliveryError(f"발송 서비스 응답 {r.status_code}", retryable=True)
        if r.status_code >= 400:
            raise DeliveryError(f"발송 거부 {r.status_code}: {r.json().get('error')}", retryable=False)
        return r.json()


def handlers(notifier: Notifier) -> dict:
    key = OutboxDispatcher.idempotency_key

    def documents_requested(ev: dict, client) -> Delivery:
        ret = client.get("TaxReturn", ev["target_pk"])
        docs = [c["pk"] for c in ev["payload"]["changes"] if c["object_type"] == "DocumentSubmission"]
        names = [client.get("DocumentSubmission", pk).document_type.doc_name for pk in docs]
        note = (ev["payload"].get("params") or {}).get("note")
        text = (f"[세무사무소] {ret.tax_kind} 신고 자료를 요청드립니다: {', '.join(names)}. "
                f"{ret.due_date.month}월 {ret.due_date.day}일까지 보내 주세요." + (f" ({note})" if note else ""))
        res = notifier.send(key(ev), ret.customer.phone, "문자", text)
        return Delivery("문자", "고객", res["message_id"], res.get("duplicate", False), text)

    def documents_request_cancelled(ev: dict, client) -> Delivery:
        ret = client.get("TaxReturn", ev["target_pk"])
        reason = (ev["payload"].get("params") or {}).get("reason") or ""
        text = f"[세무사무소] 앞서 보내 드린 {ret.tax_kind} 자료 요청을 취소합니다. ({reason})"
        res = notifier.send(key(ev), ret.customer.phone, "문자", text)
        return Delivery("문자", "고객", res["message_id"], res.get("duplicate", False), text)

    def approval_requested(ev: dict, client) -> Delivery:
        p = ev["payload"]
        expires = str(p.get("expires_at") or "-")[:16].replace("T", " ")
        text = (f"[승인 요청] {p['action']}({ev['target_pk']}) — 요청 {p['requested_by']['id']}, "
                f"변경 {p['changes']}건, 기한 {expires}")
        res = notifier.send(key(ev), "approvers:세무사", "메신저", text)
        return Delivery("메신저", "세무사", res["message_id"], res.get("duplicate", False), text)

    return {"documents_requested": documents_requested,
            "documents_request_cancelled": documents_request_cancelled,
            "approval_requested": approval_requested}


def make_dispatcher(client, notifier: Notifier, **kw) -> OutboxDispatcher:
    """발송 결과·실패를 Action(시스템 실행자)으로 온톨로지에 기록하는 작업자"""

    def delivered(ev: dict, d: Delivery) -> str | None:
        res = client.actions.apply("record_notification", ev["target_pk"], actor=SYSTEM, params={
            "event_id": ev["event_id"], "event_type": ev["event_type"], "channel": d.channel,
            "recipient": d.recipient, "external_id": d.external_id, "attempts": ev["attempts"] + 1})
        return None if res.ok else f"결과 기록 실패: {res.messages[0]}"

    def dead(ev: dict, error: str) -> None:
        channel, recipient = ROUTES[ev["event_type"]]
        client.actions.apply("report_notification_failure", ev["target_pk"], actor=SYSTEM, params={
            "event_id": ev["event_id"], "event_type": ev["event_type"], "channel": channel,
            "recipient": recipient, "attempts": ev["attempts"] + 1, "error": error})

    return OutboxDispatcher(client, handlers(notifier), on_delivered=delivered, on_dead=dead, **kw)
