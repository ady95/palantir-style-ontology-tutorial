"""C. 운영형 온톨로지 — MiniOnto (PART 4~6)

규칙·권한·승인·감사는 정의(ontology.yaml)와 엔진에 있다. 이 파일에는 업무 요청을 엔진에 넘기는 코드만 있다.
"""

from __future__ import annotations

from minionto import OntologyClient
from minionto.audit import AuditLog

REQ = "request_missing_documents"


# ---------------------------------------------------------------------- T1 조회
def missing_customers(c: OntologyClient, as_of: str, due: str) -> list[str]:
    q = c.objects("Customer").where(business_status="영업").where({"some": {"link": "tax_returns", "where": {"all": [
        {"field": "status", "op": "eq", "value": "자료수집중"},
        {"field": "due_date", "op": "gte", "value": as_of}, {"field": "due_date", "op": "lte", "value": due},
        {"some": {"link": "submissions", "where": {"field": "status", "op": "in", "value": ["미요청", "요청됨"]}}}]}}})
    return sorted(o.pk for o in q.all())


# ---------------------------------------------------------------------- T2 분류
def repeat_risk(c: OntologyClient) -> list[str]:
    q = c.objects("Customer")
    for t in ("기한후신고", "가산세부과"):
        q = q.where({"some": {"link": "risk_events", "where": {"field": "event_type", "op": "eq", "value": t}}})
    return sorted(o.pk for o in q.all())


# ---------------------------------------------------------------------- T4 업무 실행
def request_documents(c: OntologyClient, return_id: str, actor: dict, note: str | None = None,
                      key: str | None = None) -> dict:
    r = c.actions.apply(REQ, return_id, actor=actor, params={"note": note} if note else {}, idempotency_key=key)
    status = {"completed": "executed", "approval_required": "approval"}.get(r.status, "blocked")
    return {"status": status, "reason": (r.messages or [""])[0], "execution_id": r.execution_id}


# ---------------------------------------------------------------------- T5 권한
def list_customers(c: OntologyClient, actor: dict) -> list[dict]:
    return [o.properties for o in c.objects("Customer").secure(actor).all()]


# ---------------------------------------------------------------------- T6 감사
def who_changed(c: OntologyClient, execution_id: str) -> dict:
    t = AuditLog(c).trace(execution_id)
    ex = t.get("execution") or {}
    return {"actor": (ex.get("actor") or {}).get("id"), "action": ex.get("action"), "at": ex.get("created_at"),
            "changes": len(t.get("changes") or []), "events": len(t.get("events") or [])}
