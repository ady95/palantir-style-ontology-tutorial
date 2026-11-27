"""21장. 인증 · 권한 정책 · 승인 워크플로 테스트

바깥 트랜잭션 안에서 실행하고 롤백한다. 토큰 서명 키는 테스트 전용 값을 쓴다.
실행: uv run pytest -q tests/test_minionto_security.py
"""

from __future__ import annotations

import copy
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
import yaml

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from fastapi.testclient import TestClient  # noqa: E402

from minionto import Ontology, OntologyClient  # noqa: E402
from minionto.api import Served, create_app  # noqa: E402
from minionto.auth import AuthError, JwtAuth  # noqa: E402
from minionto.policy import PolicyError  # noqa: E402
from minionto.validator import validate  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
RAW = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
AS_OF = "2027-01-18"
SECRET = "test-only-secret-0123456789abcdef-0123456789"
REQ = "request_missing_documents"

TAX = {"id": "A001", "role": "세무사", "type": "human"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def _open():
    c = OntologyClient.open(TAXOS, today=AS_OF)
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


# ================================================================ 정책 정의 검증 (DB 없이)
def test_security_definition_is_validated():
    raw = copy.deepcopy(RAW)
    sec = raw["security"]
    sec["row_policies"]["own_customers"]["roles"] = ["인턴"]
    sec["row_policies"]["own_customers"]["rules"]["Task"] = {"field": "status", "op": "eq", "value": "$params.x"}
    sec["property_policies"]["personal_info"]["hidden"]["Customer"] += ["fax", "customer_id"]
    text = "\n".join(str(i) for i in validate(Ontology.model_validate(raw)).issues)
    assert "역할 '인턴' 이 roles 에 없습니다" in text
    assert "정책 조건에는 $actor·$today·$now 만 쓸 수 있습니다" in text
    assert "'Customer' 에 속성 'fax' 이 없습니다" in text
    assert "기본키 'customer_id' 는 감출 수 없습니다" in text
    assert [str(i) for i in validate(Ontology.model_validate(RAW)).issues] == []


# ================================================================ 행 정책
def test_row_policy_limits_staff(tx):
    own = scalar(tx, "SELECT count(*) FROM customers WHERE accountant_id = 'A003'")
    assert tx.objects("Customer").secure(STAFF).count() == own
    assert tx.objects("Customer").secure(TAX).count() == tx.objects("Customer").count()
    own_returns = scalar(tx, "SELECT count(*) FROM tax_returns r JOIN customers c USING (customer_id) "
                             "WHERE c.accountant_id = 'A003'")
    assert tx.objects("TaxReturn").secure(STAFF).count() == own_returns
    # 정책이 없는 타입(담당자)에서 출발해도 pivot 한 결과에는 정책이 붙는다
    assert tx.objects("Accountant").secure(STAFF).pivot("customers").count() == own
    other = tx.objects("Customer").where({"field": "accountant_id", "op": "ne", "value": "A003"}).first()
    assert tx.get("Customer", other.pk, actor=STAFF) is None
    assert tx.get("Customer", other.pk, actor=TAX) is not None


def test_actions_respect_row_policy(tx):
    def first_return(op):
        return tx.actions.candidates(REQ).where(
            {"field": "customer.accountant_id", "op": op, "value": "A003"}).order_by("return_id").first().pk
    res = tx.actions.apply(REQ, first_return("ne"), actor=STAFF)
    assert res.status == "denied" and res.messages[0].startswith("정책 'own_customers'")
    assert tx.actions.check(REQ, first_return("ne"), actor=STAFF).status == "denied"
    assert all(r.customer.accountant_id == "A003" for r in tx.actions.candidates(REQ, actor=STAFF).limit(50))
    assert tx.actions.apply(REQ, first_return("eq"), actor=STAFF, dry_run=True).status in ("completed",
                                                                                            "approval_required")
    log = tx.conn.execute("SELECT decision, kind, policy FROM minionto.policy_decisions "
                          "ORDER BY decision_id DESC LIMIT 1").fetchone()
    assert log == {"decision": "deny", "kind": "row", "policy": "own_customers"}


# ================================================================ 속성 정책
def test_property_policy_hides_personal_info(tx):
    c = tx.objects("Customer").secure(AI).first()
    assert "phone" not in c.properties and "risk_level" in c.properties
    with pytest.raises(PolicyError):
        c.phone
    for bad in (lambda: tx.objects("Customer").secure(AI).where(phone="x").count(),
                lambda: tx.objects("Customer").secure(AI).order_by("business_reg_no").first(),
                lambda: tx.objects("TaxReturn").secure(AI).rows("customer.representative")):
        with pytest.raises(PolicyError):
            bad()
    assert "phone" in tx.objects("Customer").secure(STAFF).first().properties      # 사람은 본다
    # Function 출력의 객체에서도 감춘다
    rec = tx.functions.call_as(AI, "recommend_assignee", customer="C0001")
    assert "email" not in rec.properties and rec.restricted == {"email"}
    with pytest.raises(PolicyError):                                            # 범위 밖 입력
        tx.functions.call_as(STAFF, "filing_delay_risk", customer=tx.objects("Customer").where(
            {"field": "accountant_id", "op": "ne", "value": "A003"}).first().pk)
    read = tx.conn.execute("SELECT detail FROM minionto.policy_decisions WHERE kind = 'read' AND "
                           "object_type = 'Customer' AND actor->>'type' = 'agent' "
                           "ORDER BY decision_id DESC LIMIT 1").fetchone()
    assert read["detail"]["hidden"] == ["business_reg_no", "phone", "representative"]


# ================================================================ 인증
def test_jwt_issue_and_reject():
    auth = JwtAuth(SECRET)
    tok = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=["objects:read"])
    p = auth.authenticate(tok)
    assert p.actor["id"] == "agent-01" and p.scopes == {"objects:read"} and p.actor["token_id"] == p.token_id
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    with pytest.raises(AuthError, match="만료"):
        auth.authenticate(auth.issue(sub="x", role="AI비서", actor_type="agent", scopes=[], now=past))
    with pytest.raises(AuthError):                                   # 다른 키로 서명
        JwtAuth(SECRET + "x").authenticate(tok)
    head, body, _ = tok.split(".")
    with pytest.raises(AuthError):                                   # alg=none 위조
        auth.authenticate(jwt.encode(jwt.decode(tok, options={"verify_signature": False}), None, algorithm="none"))
    with pytest.raises(AuthError):
        auth.authenticate(f"{head}.{body}.")
    with pytest.raises(AuthError, match="너무 짧"):
        JwtAuth("short")


@pytest.fixture
def api():
    client = _open()
    auth = JwtAuth(SECRET)
    served = Served.from_file(TAXOS)
    with client.conn.transaction(force_rollback=True):
        app = create_app([served], auth=auth, today=AS_OF, connect=lambda s: nullcontext(client))
        with TestClient(app) as http:
            http.engine, http.issuer = client, auth
            yield http
    client.close()


def bearer(api, actor, scopes, ttl=timedelta(hours=1)):
    tok = api.issuer.issue(sub=actor["id"], role=actor["role"], actor_type=actor["type"], scopes=scopes, ttl=ttl)
    return {"Authorization": f"Bearer {tok}"}


ALL = ["objects:read", "functions:call", "actions:apply", "approvals:decide"]
P = "/v1/ontologies/taxos"


def test_api_auth_scope_and_policy(api):
    assert api.get(P).status_code == 401
    expired = bearer(api, AI, ALL, ttl=timedelta(seconds=-10))
    assert api.get(P, headers=expired).json()["detail"].startswith("토큰이 만료")
    read_only = bearer(api, AI, ["objects:read"])
    r = api.post(f"{P}/actions/{REQ}/apply", headers=read_only, json={"target": "R00006"})
    assert r.status_code == 403 and "actions:apply" in r.json()["detail"]
    assert scalar(api.engine, "SELECT kind FROM minionto.policy_decisions ORDER BY decision_id DESC LIMIT 1") == "scope"

    me = api.get(P, headers=read_only).json()["you"]
    assert me["scopes"] == ["objects:read"] and me["hidden_properties"]["Customer"] == [
        "business_reg_no", "phone", "representative"]
    found = api.post(f"{P}/objects/Customer/search", headers=read_only, json={"limit": 1}).json()
    assert "phone" not in found["data"][0] and found["hidden"] == ["business_reg_no", "phone", "representative"]
    assert api.post(f"{P}/objects/Customer/search", headers=read_only,
                    json={"where": [{"field": "phone", "op": "eq", "value": "x"}]}).status_code == 403

    staff = bearer(api, STAFF, ALL)
    other = api.engine.objects("Customer").where({"field": "accountant_id", "op": "ne", "value": "A003"}).first().pk
    own = api.engine.objects("Customer").where(accountant_id="A003").first().pk
    assert api.get(f"{P}/objects/Customer/{other}", headers=staff).status_code == 404     # 존재도 숨김
    assert api.get(f"{P}/objects/Customer/C9999", headers=staff).status_code == 404
    assert api.get(f"{P}/objects/Customer/{own}", headers=staff).status_code == 200
    assert api.post(f"{P}/functions/filing_delay_risk", headers=staff,
                    json={"args": {"customer": other}}).status_code == 403
    total = api.post(f"{P}/objects/Customer/aggregate", headers=staff, json={}).json()["count"]
    assert total == scalar(api.engine, "SELECT count(*) FROM customers WHERE accountant_id = 'A003'")


# ================================================================ 승인 워크플로
def high_risk(c):
    return c.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": "eq", "value": "높음"}).order_by("return_id").first().pk


def test_approval_has_preview_deadline_and_events(tx):
    ret = high_risk(tx)
    res = tx.actions.apply(REQ, ret, actor=AI)
    assert res.status == "approval_required" and res.events == ["approval_requested"]
    assert any(m.startswith("승인 기한: ") for m in res.messages)
    req = tx.actions.pending_approvals()[-1]
    assert req["approval_id"] and len(req["preview"]) == len([c for c in req["preview"]]) >= 2
    assert req["expires_at"] is not None and req["overdue"] is False
    assert scalar(tx, "SELECT payload->>'approval_id' FROM minionto.outbox WHERE event_type = 'approval_requested' "
                      "ORDER BY event_id DESC LIMIT 1") == str(req["approval_id"])
    done = tx.actions.decide(str(req["approval_id"]), TAX)
    assert done.ok and len(done.changes) == len(req["preview"])
    assert scalar(tx, "SELECT payload->>'decision' FROM minionto.outbox WHERE event_type = 'approval_decided' "
                      "ORDER BY event_id DESC LIMIT 1") == "approved"


def test_approval_expires_after_deadline(tx):
    tx.actions.apply(REQ, high_risk(tx), actor=AI)
    aid = str(tx.actions.pending_approvals()[-1]["approval_id"])
    tx.conn.execute("UPDATE minionto.approval_requests SET expires_at = expires_at - interval '25 hours' "
                    "WHERE approval_id = %s", (aid,))                       # 시간이 흐른 것처럼
    assert tx.actions.pending_approvals()[-1]["overdue"] is True
    res = tx.actions.decide(aid, TAX)
    assert res.status == "rejected" and "승인 기한" in res.messages[0]
    assert scalar(tx, "SELECT status FROM minionto.approval_requests WHERE approval_id = %s", aid) == "expired"


def test_expire_stale_sweeps(tx):
    tx.actions.apply(REQ, high_risk(tx), actor=AI)
    aid = str(tx.actions.pending_approvals()[-1]["approval_id"])
    assert aid not in tx.actions.expire_stale()
    tx.conn.execute("UPDATE minionto.approval_requests SET expires_at = expires_at - interval '25 hours' "
                    "WHERE approval_id = %s", (aid,))
    assert aid in tx.actions.expire_stale()
    assert scalar(tx, "SELECT count(*) FROM minionto.outbox WHERE event_type = 'approval_expired' "
                      "AND payload->>'approval_id' = %s", aid) == 1


def test_approval_requires_same_changes_as_preview(tx):
    ret = high_risk(tx)
    tx.actions.apply(REQ, ret, actor=AI)
    req = tx.actions.pending_approvals()[-1]
    docs = [c["pk"] for c in req["preview"] if c["object_type"] == "DocumentSubmission"]
    assert len(docs) >= 2
    # 승인 전에 세무사가 자료 하나를 직접 접수 → 실행 조건은 여전히 통과하지만 바뀔 내용이 달라진다
    assert tx.actions.apply("record_document_submission", docs[0], actor=TAX).ok
    res = tx.actions.decide(str(req["approval_id"]), TAX)
    assert res.status == "rejected" and res.messages[0].startswith("승인한 변경 내용과 실제 변경이 다릅니다")
    assert f"미리보기에 있었지만 사라진 변경: update DocumentSubmission({docs[0]})" in res.messages
    assert scalar(tx, "SELECT status FROM minionto.approval_requests WHERE approval_id = %s",
                  str(req["approval_id"])) == "expired"
