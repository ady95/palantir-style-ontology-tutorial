"""19장. Ontology API · SDK 테스트

FastAPI TestClient 로 서버를 띄우지 않고 요청한다. API 가 쓰는 DB 연결을 바깥 트랜잭션으로 감싸
테스트가 끝나면 모든 변경을 롤백한다. 생성한 SDK 도 같은 TestClient 를 통해 검증한다.
실행: uv run pytest -q tests/test_minionto_api.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import nullcontext
from datetime import date
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from fastapi.testclient import TestClient  # noqa: E402

from minionto.api import Served, create_app, load_api_keys  # noqa: E402
from minionto.codegen import generate, metadata_from_file  # noqa: E402
from minionto.sdk import RemoteOntology  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
NORTHWIND = ENGINE / "ontologies" / "northwind" / "ontology.yaml"
KEYS = load_api_keys(ENGINE / "examples" / "dev_api_keys.yaml")
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
P = "/v1/ontologies/taxos"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


TAX, STAFF, AGENT = auth("dev-tax-accountant"), auth("dev-staff-a003"), auth("dev-ai-agent")


# ================================================================ DB 없이
def test_generated_sdk_compiles_and_ignores_unknown_keys(tmp_path):
    for path in (TAXOS, NORTHWIND):
        code = generate(metadata_from_file(path))
        compile(code, str(path), "exec")
    mod = _load_sdk(tmp_path)
    r = mod.TaxReturn.from_dict({"return_id": "R1", "due_date": "2027-01-25", "new_field": 1})
    assert r.due_date == date(2027, 1, 25)
    assert mod.TaxosClient.link_targets["TaxReturn"]["customer"] == "Customer"


def test_metadata_hides_physical_mapping():
    from minionto.describe import public_metadata
    meta = json.dumps(public_metadata(Served.from_file(TAXOS).onto), ensure_ascii=False)
    for internal in ('"source"', '"column"', '"implementation"', "url_env", '"table"'):
        assert internal not in meta


# ================================================================ API (DB)
@pytest.fixture(scope="module")
def served():
    return Served.from_file(TAXOS)


@pytest.fixture
def api(served):
    client = served.client(today=AS_OF)
    try:
        client.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with client.conn.transaction(force_rollback=True):
        app = create_app([served], api_keys=KEYS, today=AS_OF, connect=lambda s: nullcontext(client))
        with TestClient(app) as http:
            http.engine = client
            yield http
    client.close()


def test_auth_required(api):
    assert api.get("/health").status_code == 200
    assert api.get("/v1/ontologies").status_code == 401
    assert api.get("/v1/ontologies", headers=auth("wrong")).status_code == 401
    assert api.get("/v1/ontologies/nope", headers=TAX).status_code == 404


def test_search_matches_local_engine(api):
    body = {"where": [{"field": "status", "op": "eq", "value": "자료수집중"},
                      {"field": "customer.risk_level", "op": "eq", "value": "높음"}],
            "order_by": ["due_date"], "limit": 3}
    r = api.post(f"{P}/objects/TaxReturn/search", json=body, headers=AGENT).json()
    local = (api.engine.objects("TaxReturn").where(status="자료수집중")
             .where({"field": "customer.risk_level", "op": "eq", "value": "높음"}))
    assert r["total"] == local.count() and len(r["data"]) == min(3, r["total"])
    dues = [x["due_date"] for x in r["data"]]
    assert dues == sorted(dues)

    pivot = api.post(f"{P}/objects/TaxReturn/search", headers=AGENT, json={
        "where": body["where"], "steps": [{"pivot": "customer"}], "select": ["company_name", "accountant.name"]})
    assert pivot.json()["object_type"] == "Customer"
    assert set(pivot.json()["data"][0]) == {"company_name", "accountant.name"}


def test_bad_query_is_400_and_unknown_type_404(api):
    r = api.post(f"{P}/objects/TaxReturn/search", headers=AGENT,
                 json={"where": [{"field": "no_such", "op": "eq", "value": 1}]})
    assert r.status_code == 400 and "no_such" in r.json()["detail"]
    assert api.post(f"{P}/objects/Nope/search", json={}, headers=AGENT).status_code == 404


def test_object_links_and_aggregate(api):
    rid = api.engine.objects("TaxReturn").order_by("return_id").first().pk
    obj = api.get(f"{P}/objects/TaxReturn/{rid}", headers=AGENT).json()
    assert obj["pk"] == rid and "customer" in obj["links"]
    assert api.get(f"{P}/objects/TaxReturn/NOPE", headers=AGENT).status_code == 404
    one = api.get(f"{P}/objects/TaxReturn/{rid}/links/customer", headers=AGENT).json()
    assert one["many"] is False and one["data"]["object_type"] == "Customer"
    many = api.get(f"{P}/objects/TaxReturn/{rid}/links/submissions?limit=2", headers=AGENT).json()
    assert many["many"] is True and len(many["data"]) <= 2

    agg = api.post(f"{P}/objects/TaxReturn/aggregate", headers=AGENT, json={"count_by": ["status"]}).json()
    assert sum(g["count"] for g in agg["groups"]) == api.engine.objects("TaxReturn").count()


def test_function_call(api):
    r = api.post(f"{P}/functions/has_capacity", headers=AGENT, json={"args": {"accountant": "A001"}})
    assert r.status_code == 200 and isinstance(r.json()["result"], bool)
    bad = api.post(f"{P}/functions/has_capacity", headers=AGENT, json={"args": {}})
    assert bad.status_code == 422


def test_actor_comes_from_token(api):
    cands = api.get(f"{P}/actions/{REQ}/candidates?limit=50", headers=AGENT).json()
    normal = next(c["pk"] for c in cands["data"]
                  if (cust := api.engine.get("TaxReturn", c["pk"]).customer).risk_level != "높음"
                  and cust.accountant_id != "A003")
    # 본문에 actor 를 넣어도 무시되고 토큰의 실행자(실무자 A003)로 검사된다
    r = api.post(f"{P}/actions/{REQ}/apply", headers=STAFF,
                 json={"target": normal, "params": {}, "actor": {"role": "세무사"}})
    assert r.status_code == 403 and r.json()["status"] == "denied"

    dry = api.post(f"{P}/actions/{REQ}/apply", headers=AGENT, json={"target": normal, "dry_run": True})
    assert dry.status_code == 200 and dry.json()["dry_run"] and dry.json()["changes"]
    done = api.post(f"{P}/actions/{REQ}/apply", headers=AGENT, json={"target": normal})
    assert done.json()["status"] == "completed"
    again = api.post(f"{P}/actions/{REQ}/apply", headers=AGENT, json={"target": normal})
    assert again.status_code == 409 and again.json()["status"] == "rejected"
    assert api.post(f"{P}/actions/nope/apply", headers=AGENT, json={"target": "x"}).status_code == 404


def test_approval_over_api(api):
    risky = (api.engine.actions.candidates(REQ, actor=KEYS["dev-ai-agent"])
             .where({"field": "customer.risk_level", "op": "eq", "value": "높음"}).order_by("return_id").first())
    r = api.post(f"{P}/actions/{REQ}/apply", headers=AGENT, json={"target": risky.pk})
    assert r.status_code == 202
    aid = r.json()["approval_id"]
    assert aid in [a["approval_id"] for a in api.get(f"{P}/approvals", headers=TAX).json()["data"]]
    assert aid not in [a["approval_id"] for a in api.get(f"{P}/approvals", headers=STAFF).json()["data"]]
    ok = api.post(f"{P}/approvals/{aid}/decide", headers=TAX, json={"approve": True})
    assert ok.status_code == 200 and ok.json()["status"] == "completed"
    assert api.post(f"{P}/approvals/{aid}/decide", headers=TAX, json={}).status_code == 409
    assert api.post(f"{P}/approvals/not-a-uuid/decide", headers=TAX, json={}).status_code == 404


# ================================================================ SDK (DB)
def _load_sdk(tmp_path):
    out = tmp_path / "taxos_sdk.py"
    out.write_text(generate(metadata_from_file(TAXOS)), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("taxos_sdk_test", out)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                 # dataclass 가 모듈을 찾을 수 있게
    spec.loader.exec_module(mod)
    return mod


def test_generated_sdk_end_to_end(api, tmp_path):
    mod = _load_sdk(tmp_path)
    sdk = mod.TaxosClient("http://testserver", token="dev-ai-agent", http=api)
    assert sdk.check_version() == mod.TaxosClient.version

    rows = sdk.TaxReturn.where(status="자료수집중").order_by("due_date").limit(2).all()
    assert all(isinstance(r, mod.TaxReturn) and isinstance(r.due_date, date) for r in rows)
    customers = sdk.TaxReturn.where(return_id=rows[0].return_id).pivot("customer").all()
    assert isinstance(customers[0], mod.Customer)
    assert sdk.TaxReturn.where(status="자료수집중").count() == \
        api.engine.objects("TaxReturn").where(status="자료수집중").count()

    target = sdk.candidates(REQ, limit=1)[0].pk
    res = sdk.request_missing_documents(target, note="SDK 테스트", dry_run=True)
    assert res.status in ("completed", "approval_required")

    plain = RemoteOntology("http://testserver", "taxos", token="dev-ai-agent", http=api)
    assert plain.objects("Customer").limit(1).first()["customer_id"]       # 타입 없는 SDK 는 dict


# ================================================================ 20장: 상태 전이
def test_available_and_expect_over_api(api, tmp_path):
    ret = api.engine.objects("TaxReturn").where(status="검토중").order_by("return_id").first().pk
    av = api.get(f"{P}/objects/TaxReturn/{ret}/actions", headers=TAX).json()
    assert av["state"]["value"] == "검토중" and av["state"]["terminal"] is False
    status = {a["action"]: a["status"] for a in av["actions"]}
    assert status["complete_filing"] == "ready" and status["return_to_collection"] == "needs_input"
    assert api.get(f"{P}/objects/TaxReturn/R99999/actions", headers=TAX).status_code == 404

    stale = api.post(f"{P}/actions/complete_filing/apply", headers=TAX,
                     json={"target": ret, "expect": {"status": "자료수집중"}})
    assert stale.status_code == 409 and stale.json()["status"] == "rejected"
    done = api.post(f"{P}/actions/complete_filing/apply", headers=TAX,
                    json={"target": ret, "expect": {"status": "검토중"}})
    assert done.status_code == 200
    assert {"object_type": "TaxReturn", "pk": ret, "property": "status", "from": "검토중", "to": "신고완료"} \
        in done.json()["transitions"]
    hist = api.get(f"{P}/objects/TaxReturn/{ret}/history", headers=TAX).json()["data"]
    assert [(h["from_state"], h["to_state"]) for h in hist] == [("검토중", "신고완료")]

    # SDK: 상태 머신이 독스트링에, available·history·expect 가 메서드로
    code = generate(metadata_from_file(str(TAXOS)))
    assert "상태 전이: TaxReturn.status 검토중 → 신고완료" in code
    out = tmp_path / "taxos_sdk20.py"
    out.write_text(code, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("taxos_sdk20", out)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["taxos_sdk20"] = mod
    spec.loader.exec_module(mod)
    sdk = mod.TaxosClient("http://testserver", token="dev-tax-accountant", http=api)
    assert sdk.available("TaxReturn", ret).state["terminal"] is True
    assert sdk.history("TaxReturn", ret)[0].to_state == "신고완료"
    assert sdk.complete_filing(ret, expect={"status": "검토중"}).status == "rejected"
