"""실습용 모의 발송 서비스 (23장) — 문자·메신저 발송 업체의 API 를 흉내 낸다

실제로 아무것도 보내지 않고, 받은 메시지를 메모리에 쌓아 둔다.
- Idempotency-Key 가 같은 요청은 다시 보내지 않고 처음 메시지 ID 를 돌려준다 (duplicate: true)
- fail_next 만큼 503(일시 장애)을 돌려준다 — 재시도 실습
- reject_numbers 에 있는 번호는 400(수신 거부)을 돌려준다 — 영구 실패 실습

    app = create_mock_notifier()
    http = TestClient(app)            # 포트 없이 같은 프로세스에서 호출
    app.state.box["fail_next"] = 2    # 다음 두 번은 503
"""

from __future__ import annotations

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse


def mask(number: str) -> str:
    """로그·출력용: 010-0000-0008 → 010-****-0008"""
    parts = number.split("-")
    return "-".join([parts[0], "*" * len(parts[1]), parts[2]]) if len(parts) == 3 else number


def create_mock_notifier(*, fail_next: int = 0, reject_numbers: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="모의 발송 서비스")
    box = {"fail_next": fail_next, "reject": set(reject_numbers or ()), "messages": {}, "calls": 0}
    app.state.box = box

    @app.post("/v1/messages")
    def send(body: dict, idempotency_key: str = Header(...)):
        box["calls"] += 1
        if idempotency_key in box["messages"]:
            return {"message_id": box["messages"][idempotency_key]["message_id"], "duplicate": True}
        if box["fail_next"] > 0:
            box["fail_next"] -= 1
            return JSONResponse({"error": "일시 장애"}, status_code=503)
        if body["to"] in box["reject"]:
            return JSONResponse({"error": "수신 거부 번호"}, status_code=400)
        mid = f"MSG-{len(box['messages']) + 1:04d}"
        box["messages"][idempotency_key] = {"message_id": mid, "to": mask(body["to"]), **{
            k: body[k] for k in ("channel", "text")}}
        return {"message_id": mid, "duplicate": False}

    @app.get("/v1/messages")
    def messages():
        return list(box["messages"].values())

    return app


# uvicorn 으로 띄울 때: uv run uvicorn mock_notifier:app --app-dir examples --port 8900
app = create_mock_notifier()
