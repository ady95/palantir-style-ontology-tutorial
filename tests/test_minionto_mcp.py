"""26장. MiniOnto MCP 서버 테스트 (같은 프로세스의 MCP 클라이언트, 바깥 트랜잭션은 롤백)

실행: uv run pytest -q tests/test_minionto_mcp.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

mcp = pytest.importorskip("mcp")
from fastapi.testclient import TestClient  # noqa: E402
from mcp import Client  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.agent import SessionStore  # noqa: E402
from minionto.auth import JwtAuth  # noqa: E402
from minionto.mcp_server import MiniOntoMCP  # noqa: E402
from minionto.policy import PolicyError  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
ALL = ["objects:read", "functions:call", "actions:apply"]
AUTH = JwtAuth("test-secret-for-minionto-mcp-0123456789")


@pytest.fixture
def tx():
    c = OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        yield c
    c.close()


def token(scopes=ALL, sub="agent-01", role="AI비서") -> str:
    return AUTH.issue(sub=sub, role=role, actor_type="agent", scopes=scopes)


def server(tx, tok, **kw):
    return MiniOntoMCP(TAXOS, auth=AUTH, today=AS_OF, stdio_token=tok, client=tx, **kw).server()


def run(coro):
    return asyncio.run(coro)


def low_risk(tx) -> str:
    return tx.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk


def test_tools_depend_on_token_scope(tx):
    async def names(tok):
        async with Client(server(tx, tok)) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    full, read = run(names(token())), run(names(token(["objects:read"])))
    assert set(full) == {"find_objects", "get_object", "call_function", "list_actions", "check_action", "run_action"}
    assert set(read) == {"find_objects", "get_object", "list_actions", "check_action"}
    assert full["run_action"].annotations.read_only_hint is False
    assert full["get_object"].annotations.read_only_hint is True
    assert "missing_documents(tax_return)" in full["call_function"].description
    enum = full["run_action"].input_schema["properties"]["action"]["enum"]
    assert enum == [REQ, "start_review"]                                # 실행자의 역할로 거른 Action


def test_results_errors_resources_prompts(tx):
    ret = low_risk(tx)

    async def go():
        async with Client(server(tx, token())) as c:
            ok = await c.call_tool("get_object", {"object_type": "TaxReturn", "id": ret})
            denied = await c.call_tool("run_action", {"action": "complete_filing", "target": ret})
            bad = await c.call_tool("find_objects", {"object_type": "Nope"})
            ctx = (await c.read_resource("minionto://context")).contents[0].text
            prompt = (await c.get_prompt("ontology_agent")).messages[0].content.text
            return ok, denied, bad, ctx, prompt
    ok, denied, bad, ctx, prompt = run(go())
    assert ok.is_error is False and ok.structured_content["object"]["status"] == "자료수집중"
    assert '"status": "ok"' in ok.content[0].text                       # 같은 내용을 text 로도
    assert denied.is_error is True and denied.structured_content["status"] == "denied"
    assert bad.is_error is True and bad.structured_content["status"] == "invalid"
    assert ctx.startswith("실행자 agent-01") and "phone" not in ctx.split("볼 수 없는 속성")[0]
    assert prompt.startswith("세무사무소 온톨로지") and "# 실행할 수 있는 Action" in prompt


def test_session_survives_connections_and_is_per_token(tx):
    ret, tok = low_risk(tx), token()

    async def call(t, name, args):
        async with Client(server(tx, t)) as c:                         # 호출마다 새 연결
            return (await c.call_tool(name, args)).structured_content
    assert run(call(tok, "get_object", {"object_type": "TaxReturn", "id": ret}))["status"] == "ok"
    first = run(call(tok, "run_action", {"action": REQ, "target": ret}))
    assert first["status"] == "completed"
    again = run(call(tok, "run_action", {"action": REQ, "target": ret}))
    assert again["status"] == "replayed" and again["execution_id"] == first["execution_id"]
    assert run(call(token(), "run_action", {"action": REQ, "target": ret}))["status"] == "read_first"
    jti = AUTH.authenticate(tok).token_id
    assert first["idempotency_key"] == f"agent:{jti}:{REQ}:{ret}"
    with pytest.raises(PolicyError):                                    # 남의 세션은 이어 쓰지 못한다
        SessionStore(tx).open(jti, {"id": "agent-02", "role": "AI비서", "type": "agent"})


def test_read_only_token_cannot_run_and_is_told_so(tx):
    ready = tx.actions.candidates("start_review", actor=AI).order_by("return_id").first().pk
    tok = token(["objects:read"])

    async def go():
        async with Client(server(tx, tok)) as c:
            await c.call_tool("get_object", {"object_type": "TaxReturn", "id": ready})
            chk = await c.call_tool("check_action", {"action": "start_review", "target": ready})
            run_ = await c.call_tool("run_action", {"action": "start_review", "target": ready})
            return chk.structured_content, run_
    chk, run_ = run(go())
    assert chk["status"] == "ready" and "actions:apply" in chk["next_step"]
    assert run_.is_error and "actions:apply" in run_.structured_content["messages"][0]
    assert tx.get("TaxReturn", ready).status == "자료수집중"


def test_confirm_mode_waits_for_a_human(tx):
    ret, tok = low_risk(tx), token()

    async def go():
        async with Client(server(tx, tok, confirm=True)) as c:
            await c.call_tool("get_object", {"object_type": "TaxReturn", "id": ret})
            return (await c.call_tool("run_action", {"action": REQ, "target": ret})).structured_content
    out = run(go())
    assert out["status"] == "confirmation_required"
    store = SessionStore(tx)
    assert [p["token"] for p in store.pending()] == [out["token"]]
    jti = AUTH.authenticate(tok).token_id
    s = store.open(jti, AI, confirm=True)
    done = s.confirm(out["token"], by={"id": "A001", "role": "세무사", "type": "human"})
    store.save(jti, s)
    assert done["status"] == "completed" and store.pending() == []
    assert s.confirmations[-1]["by"]["id"] == "A001"


def test_http_requires_bearer_token(tx):
    app = MiniOntoMCP(TAXOS, auth=AUTH, today=AS_OF, client=tx).http_app()
    with TestClient(app) as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert http.post("/mcp", json=body).status_code == 401
        r = http.post("/mcp", json=body, headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401 and "유효하지 않은 토큰" in r.json()["detail"]
        other = JwtAuth("another-secret-key-0123456789-abcdef").issue(
            sub="agent-01", role="AI비서", actor_type="agent", scopes=ALL)
        assert http.post("/mcp", json=body, headers={"Authorization": f"Bearer {other}"}).status_code == 401
