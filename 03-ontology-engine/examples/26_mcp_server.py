"""26장. MiniOnto MCP 서버

    uv run python 03-ontology-engine/examples/26_mcp_server.py          # 1~4단계
    uv run python 03-ontology-engine/examples/26_mcp_server.py --llm    # 5단계까지 (OpenAI 호환 API)

1. 토큰마다 다른 도구 목록 — 실행자의 역할·정책 + 토큰 범위(scope)
2. 도구 호출과 결과 — content(text) · structuredContent · isError, 리소스와 프롬프트
3. 세션 이어 가기 — 연결이 바뀌어도 같은 토큰(업무)은 같은 세션, 사람의 확인
4. Streamable HTTP — Bearer 토큰 없으면 401
5. MCP 호스트 — LLM 이 MCP 도구로 업무 요청을 처리한다

MCP 클라이언트는 서버에 같은 프로세스 안에서 연결하고(4단계는 HTTP), 모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from dotenv import load_dotenv  # noqa: E402
from mcp import Client  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.agent import SessionStore  # noqa: E402
from minionto.auth import JwtAuth  # noqa: E402
from minionto.mcp_server import MiniOntoMCP  # noqa: E402

load_dotenv(ENGINE.parent / ".env")
TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
ALL = ["objects:read", "functions:call", "actions:apply"]
TAX = {"id": "A001", "role": "세무사", "type": "human"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def short(text: str, width: int = 130) -> str:
    return text if len(text) <= width else text[:width] + " …"


async def call(c: Client, name: str, args: dict) -> dict:
    r = await c.call_tool(name, args)
    out = r.structured_content or {}
    print(f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})")
    print(f"  → isError={r.is_error} status={out.get('status')} {short(json.dumps(out.get('messages', ''), ensure_ascii=False), 100)}")
    return out


AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


async def main_async(onto: OntologyClient, use_llm: bool) -> None:
    auth = JwtAuth()
    with onto.conn.transaction(force_rollback=True):        # 1~4단계 변경은 여기서 되돌린다
        await steps_1_to_4(onto, auth)
    if use_llm:
        await step_5(onto, auth)
    else:
        print("\n(5단계는 --llm 으로 실행합니다)")


def make_server(onto: OntologyClient, auth: JwtAuth, token: str, **kw) -> MiniOntoMCP:
    return MiniOntoMCP(TAXOS, auth=auth, today=AS_OF, stdio_token=token, client=onto, **kw)


def low_risk(onto: OntologyClient) -> str:
    return onto.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk


async def steps_1_to_4(onto: OntologyClient, auth: JwtAuth) -> None:
    def server(token: str, **kw) -> MiniOntoMCP:
        return make_server(onto, auth, token, **kw)

    agent_tok = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=ALL)
    reader_tok = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=["objects:read"])
    normal = low_risk(onto)

    # ------------------------------------------------------------------ 1
    section("1. 토큰마다 다른 도구 목록")
    for label, tok in (("AI비서 · 전체 범위", agent_tok), ("AI비서 · objects:read 만", reader_tok)):
        async with Client(server(tok).server()) as c:
            tools = (await c.list_tools()).tools
            print(f"{label:22s} {len(tools)}개: {', '.join(t.name for t in tools)}")
    async with Client(server(agent_tok).server()) as c:
        for t in (await c.list_tools()).tools:
            a = t.annotations
            print(f"  {t.name:14s} readOnly={a.read_only_hint!s:5s} destructive={a.destructive_hint!s:5s} "
                  f"idempotent={a.idempotent_hint}")
        info = c.session.server_info if hasattr(c, "session") else None
        print("서버:", getattr(info, "name", "minionto-taxos"))

    # ------------------------------------------------------------------ 2
    section(f"2. 도구 호출과 결과 — {normal}")
    async with Client(server(agent_tok).server()) as c:
        r = await c.call_tool("get_object", {"object_type": "TaxReturn", "id": normal})
        print("get_object content[0].text:", short(r.content[0].text))
        print("get_object structuredContent.status:", r.structured_content["status"], "· isError:", r.is_error)
        await call(c, "run_action", {"action": "complete_filing", "target": normal})
        await call(c, "check_action", {"action": "start_review", "target": normal})
        await call(c, "check_action", {"action": REQ, "target": normal})
        res = await c.list_resources()
        print("리소스:", [str(x.uri) for x in res.resources])
        ctx = (await c.read_resource("minionto://context")).contents[0].text
        print(f"  minionto://context  {len(ctx):,}자 — 첫 줄: {ctx.splitlines()[0]}")
        pr = await c.get_prompt("ontology_agent")
        print(f"프롬프트 ontology_agent  {len(pr.messages[0].content.text):,}자 — 첫 줄: "
              f"{pr.messages[0].content.text.splitlines()[0]}")
        print(f"서버 지침(instructions) {len(server(agent_tok).server().instructions or ''):,}자")
    async with Client(server(reader_tok).server()) as c:
        await call(c, "run_action", {"action": REQ, "target": normal})

    # ------------------------------------------------------------------ 3
    section("3. 세션 이어 가기 — 토큰 하나 = 업무 하나")
    async with Client(server(agent_tok).server()) as c:          # 연결 1: 읽기만
        await call(c, "get_object", {"object_type": "TaxReturn", "id": normal})
    print("  (연결 1 종료)")
    async with Client(server(agent_tok).server()) as c:          # 연결 2: 같은 토큰으로 실행
        await call(c, "run_action", {"action": REQ, "target": normal})
        await call(c, "run_action", {"action": REQ, "target": normal})
    other = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=ALL)
    async with Client(server(other).server()) as c:              # 새 토큰 = 새 업무
        await call(c, "run_action", {"action": REQ, "target": normal})
    jti = auth.authenticate(agent_tok).token_id
    row = onto.conn.execute("SELECT state FROM minionto.agent_sessions WHERE session_key = %s", (jti,)).fetchone()
    print(f"저장된 세션 {jti[:8]}…: 읽은 대상", [f"{t}:{pk}" for t, pk, _ in row["state"]["seen"]],
          "· 멱등 키", [s["key"].replace(jti, jti[:8] + "…") for _, _, s in row["state"]["keys"]],
          "· 도구 호출", len(row["state"]["calls"]), "회")

    print("\n확인 모드 서버 (--confirm):")
    nxt = low_risk(onto)
    ctok = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=ALL)
    async with Client(server(ctok, confirm=True).server()) as c:
        await call(c, "get_object", {"object_type": "TaxReturn", "id": nxt})
        out = await call(c, "run_action", {"action": REQ, "target": nxt})
    store = SessionStore(onto)
    for p in store.pending():
        print(f"  사람이 보는 확인 대기: {p['action']}({p['target']}) 토큰 {p['token']} (에이전트 {p['actor']})")
    key = auth.authenticate(ctok).token_id
    s = store.open(key, {"id": "agent-01", "role": "AI비서", "type": "agent"}, confirm=True)
    done = s.confirm(out["token"], by=TAX)
    store.save(key, s)
    print(f"  세무사 {TAX['id']} 확인 → {done['status']} ({done['messages'][0]}), 확인 기록: "
          f"by={s.confirmations[-1]['by']['id']}")

    # ------------------------------------------------------------------ 4
    section("4. Streamable HTTP — 요청마다 Bearer 토큰")
    import httpx2
    import uvicorn
    from mcp.client.streamable_http import streamable_http_client
    app = MiniOntoMCP(TAXOS, auth=auth, today=AS_OF, client=onto).http_app()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8766, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    url = "http://127.0.0.1:8766/mcp"
    for label, headers in (("토큰 없음", {}), ("위조 토큰", {"Authorization": "Bearer x.y.z"})):
        r = httpx2.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
        print(f"{label:8s} → HTTP {r.status_code} {r.json()['detail']}")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {reader_tok}"}) as hc:
        async with Client(streamable_http_client(url, http_client=hc)) as c:
            tools = (await c.list_tools()).tools
            print(f"objects:read 토큰 → 도구 {len(tools)}개: {', '.join(t.name for t in tools)}")
            await call(c, "find_objects", {"object_type": "TaxReturn", "limit": 1,
                                           "where": [{"field": "status", "op": "eq", "value": "자료수집중"}]})
    srv.should_exit = True
    th.join()



async def step_5(onto: OntologyClient, auth: JwtAuth) -> None:
    from minionto.llm import ChatClient, run_mcp_agent

    def server(token: str, **kw) -> MiniOntoMCP:
        return make_server(onto, auth, token, **kw)

    chat = ChatClient()
    section("5. MCP 호스트 — LLM 이 MCP 도구를 쓴다")
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    risky = onto.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": "eq", "value": "높음"}).order_by("return_id").first()
    ready = onto.actions.candidates("start_review", actor=AI).order_by("return_id").first().pk
    cases = [("자료 요청", f"{low_risk(onto)} 신고에 빠진 자료가 있으면 고객에게 요청해 줘.", ALL),
             ("고위험 고객", f"{risky.customer_id} 고객의 자료수집중인 신고에 빠진 자료를 요청해 줘.", ALL),
             ("읽기 전용 토큰", f"{ready} 신고서 검토를 시작해 줘.", ["objects:read"])]
    for title, req, scopes in cases:
        print(f"\n## {title}: {req}  (토큰 범위 {', '.join(scopes)})")
        for i in range(repeat):
            tok = auth.issue(sub="agent-01", role="AI비서", actor_type="agent", scopes=scopes)
            with onto.conn.transaction(force_rollback=True):
                async with Client(server(tok).server()) as c:
                    res = await run_mcp_agent(chat, c, req, on_step=(lambda it: print(
                        f"  [{it['step']}] {it['tool']}({it['args']}) → {it['result'].get('status')}"
                        f"{' (isError)' if it['is_error'] else ''}")) if i == 0 else None)
            answer = " ".join((res["answer"] or res["stopped"] or "").split())
            tokline = f"토큰 입력 {res['usage']['prompt_tokens']:,} 출력 {res['usage']['completion_tokens']:,}"
            if i == 0:
                print(f"  답: {answer}\n  도구 호출 {len(res['trace'])}, {tokline}")
            else:
                seq = " ".join(f"{t['tool']}:{t['result'].get('status')}" for t in res["trace"]) or "(도구 호출 없음)"
                print(f"  반복 {i + 1}: {seq} | {tokline} | {answer[:70]}")
    chat.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with OntologyClient.open(TAXOS, today=AS_OF) as onto, onto.conn.transaction(force_rollback=True):
        onto.actions.ensure_tables()
        asyncio.run(main_async(onto, "--llm" in sys.argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
