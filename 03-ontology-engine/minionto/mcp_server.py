"""MiniOnto MCP 서버 — 25장의 에이전트 도구를 Model Context Protocol 로 내보낸다 (26장)

    MINIONTO_TOKEN=<에이전트 토큰> python -m minionto mcp ontologies/taxos/ontology.yaml          # stdio
    python -m minionto mcp ontologies/taxos/ontology.yaml --http --port 8765                     # Streamable HTTP

- 인증: 21장의 JWT. stdio 는 환경 변수 MINIONTO_TOKEN (서버를 띄운 사람의 자격), HTTP 는 요청마다
  Authorization: Bearer. 토큰이 없거나 틀리면 HTTP 401 — MCP 메시지까지 가지 않는다.
- 도구 목록은 토큰마다 다르다: 실행자의 역할·정책(25장)과 토큰 범위(scope)로 거른다.
- 세션: 토큰 ID(jti)가 세션 키다. 읽은 상태·멱등 키·확인 대기를 DB(minionto.agent_sessions)에 두므로
  연결이 바뀌거나 서버가 재시작해도 같은 업무를 이어 간다.
- 결과: 25장의 JSON 을 text 와 structuredContent 로 함께 보내고, 실패면 isError=true.
- 리소스 minionto://context (이 실행자의 컨텍스트), 프롬프트 ontology_agent (업무 원칙).
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server

from .agent import AgentSession, SessionStore
from .auth import AuthError, JwtAuth, Principal
from .objects import OntologyClient
from .policy import PolicyError

# 도구 → 필요한 토큰 범위 (21장의 SCOPES)
TOOL_SCOPES = {"find_objects": "objects:read", "get_object": "objects:read", "list_actions": "objects:read",
               "check_action": "objects:read", "call_function": "functions:call", "run_action": "actions:apply"}
READ_ONLY = {"find_objects", "get_object", "list_actions", "check_action", "call_function"}
# isError=false 인 결과 — 업무상 정상적으로 끝난 것 (승인 대기·확인 대기·입력 필요 포함)
OK = {"ok", "ready", "preview", "completed", "replayed", "approval_required", "confirmation_required", "needs_input"}

GUIDELINES = """세무사무소 온톨로지(MiniOnto) 도구를 쓰는 원칙:
- 데이터는 도구로 확인하고 추측하지 않는다. 도구 결과에 없는 값을 지어내지 않는다.
- 무엇을 바꾸기 전에는 get_object 로 대상을 읽고, check_action 이나 list_actions 로 지금 할 수 있는지 확인한다.
- 도구 결과의 next_step 을 따른다. 권한이 없거나 규칙상 안 되는 일은 우회하지 말고 이유를 설명한다.
- 사용자에게는 한국어로 짧게, 무엇을 했고 무엇이 남았는지 답한다."""


class BearerAuth:
    """HTTP 요청의 Bearer 토큰을 검증하는 ASGI 미들웨어. 통과하면 scope['state']['principal']"""

    def __init__(self, app, auth: JwtAuth, path: str = "/mcp"):
        self.app, self.auth, self.path = app, auth, path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.path):
            header = dict(scope.get("headers") or {}).get(b"authorization", b"").decode()
            try:
                if not header.lower().startswith("bearer "):
                    raise AuthError("Authorization: Bearer <토큰> 헤더가 필요합니다")
                principal = self.auth.authenticate(header[7:].strip())
            except AuthError as e:
                body = json.dumps({"error": "unauthorized", "detail": str(e)}, ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="minionto"')]})
                await send({"type": "http.response.body", "body": body})
                return
            scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


class MiniOntoMCP:
    def __init__(self, ontology: str | Path, *, auth: JwtAuth | None = None, url: str | None = None,
                 today: str | None = None, confirm: bool = False, stdio_token: str | None = None,
                 client: OntologyClient | None = None):
        """client: 테스트·예제용 — 주면 요청마다 연결을 새로 열지 않고 이 클라이언트(바깥 트랜잭션)를 쓴다"""
        self.path, self.url, self.today, self.confirm = Path(ontology), url, today, confirm
        self.shared = client
        from dotenv import load_dotenv
        load_dotenv()
        self.auth = auth or JwtAuth()
        self.stdio_principal: Principal | None = None
        if stdio_token is not None:                       # stdio: 서버를 띄울 때 한 번 인증 (실패하면 시작하지 않음)
            self.stdio_principal = self.auth.authenticate(stdio_token)
        with self._client() as c:
            SessionStore(c)                               # 세션 테이블 준비
            self.name = c.onto.ontology.name

    # ------------------------------------------------------------------ 공통
    def _client(self):
        if self.shared is not None:
            return nullcontext(self.shared)
        return OntologyClient.open(self.path, self.url, today=self.today)

    def principal(self, ctx) -> Principal:
        req = getattr(ctx, "request", None)
        if req is not None:
            p = getattr(req, "state", None) and getattr(req.state, "principal", None)
            if p is None:
                raise AuthError("인증되지 않은 요청입니다")
            return p
        if self.stdio_principal is None:
            raise AuthError("stdio 서버에 MINIONTO_TOKEN 이 없습니다")
        return self.stdio_principal

    @staticmethod
    def session_key(p: Principal) -> str:
        return p.token_id or f"{p.actor['id']}:local"

    def _session(self, c: OntologyClient, p: Principal) -> tuple[SessionStore, AgentSession]:
        store = SessionStore(c)
        return store, store.open(self.session_key(p), p.actor, confirm=self.confirm)

    # ------------------------------------------------------------------ 도구 (동기, 스레드에서 실행)
    def list_tools(self, p: Principal) -> list[types.Tool]:
        with self._client() as c:
            s = AgentSession(c, p.actor)
            out = []
            for t in s.tools():
                f = t["function"]
                if not p.has(TOOL_SCOPES[f["name"]]):
                    continue                                    # 토큰 범위 밖 도구는 목록에서부터 뺀다
                ro = f["name"] in READ_ONLY
                desc = f["description"]
                if f["name"] == "call_function":               # args 는 자유 형식이므로 입력 이름을 설명에
                    desc += " 입력: " + "; ".join(
                        f"{n}({', '.join(fn.inputs)})" for n, fn in c.onto.functions.items()) + \
                        " — 예: {name: missing_documents, args: {tax_return: R00006}}"
                out.append(types.Tool(
                    name=f["name"], description=desc, input_schema=f["parameters"],
                    annotations=types.ToolAnnotations(read_only_hint=ro, destructive_hint=not ro,
                                                      idempotent_hint=True, open_world_hint=False)))
            return out

    def call_tool(self, p: Principal, name: str, args: dict) -> dict:
        scope = TOOL_SCOPES.get(name)
        with self._client() as c:
            if scope and not p.has(scope):
                c.policy.log(p.actor, "deny", "scope", detail={"scope": scope, "tool": name, "via": "mcp"})
                return {"status": "denied", "messages": [f"토큰 범위(scope)에 '{scope}' 가 없습니다"],
                        "next_step": "이 도구는 쓸 수 없습니다. 사용자에게 알리세요."}
            store, s = self._session(c, p)
            out = s.call(name, args)
            store.save(self.session_key(p), s)
            if out.get("status") in ("ready", "approval_required") and name != "run_action" \
                    and not p.has("actions:apply"):            # 안내가 쓸 수 없는 도구를 가리키지 않게
                out["next_step"] = ("이 토큰에는 실행 권한(actions:apply)이 없습니다. 실행할 수 있다는 판단만 "
                                    "사용자에게 전하고, 실행은 권한 있는 사람에게 맡기세요.")
            return out

    def context(self, p: Principal) -> str:
        with self._client() as c:
            return AgentSession(c, p.actor).context_text()

    # ------------------------------------------------------------------ MCP 서버
    def server(self) -> Server:
        async def on_list_tools(ctx, params):
            p = self.principal(ctx)
            return types.ListToolsResult(tools=await anyio.to_thread.run_sync(self.list_tools, p))

        async def on_call_tool(ctx, params):
            p = self.principal(ctx)
            try:
                out = await anyio.to_thread.run_sync(self.call_tool, p, params.name, dict(params.arguments or {}))
            except (PolicyError, AuthError) as e:
                out = {"status": "denied", "messages": [str(e)]}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(out, ensure_ascii=False))],
                structured_content=out, is_error=out.get("status") not in OK)

        async def on_list_resources(ctx, params):
            return types.ListResourcesResult(resources=[types.Resource(
                uri="minionto://context", name="context", title="이 실행자의 온톨로지 컨텍스트",
                description="볼 수 있는 객체·속성·링크, 쓸 수 있는 Function, 실행할 수 있는 Action",
                mime_type="text/plain")])

        async def on_read_resource(ctx, params):
            p = self.principal(ctx)
            text = await anyio.to_thread.run_sync(self.context, p)
            return types.ReadResourceResult(contents=[types.TextResourceContents(
                uri=str(params.uri), mime_type="text/plain", text=text)])

        async def on_list_prompts(ctx, params):
            return types.ListPromptsResult(prompts=[types.Prompt(
                name="ontology_agent", title="온톨로지 업무 에이전트",
                description="도구 사용 원칙과 이 실행자의 컨텍스트를 담은 시스템 지침")])

        async def on_get_prompt(ctx, params):
            p = self.principal(ctx)
            text = GUIDELINES + "\n\n# 온톨로지\n" + await anyio.to_thread.run_sync(self.context, p)
            return types.GetPromptResult(description="온톨로지 업무 에이전트", messages=[
                types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))])

        instructions = GUIDELINES
        if self.stdio_principal is not None:             # stdio 는 실행자가 정해져 있으므로 컨텍스트까지
            instructions += "\n\n# 온톨로지\n" + self.context(self.stdio_principal)
        return Server(f"minionto-{self.name}", version="0.1.0", instructions=instructions,
                      on_list_tools=on_list_tools, on_call_tool=on_call_tool,
                      on_list_resources=on_list_resources, on_read_resource=on_read_resource,
                      on_list_prompts=on_list_prompts, on_get_prompt=on_get_prompt)

    async def run_stdio(self) -> None:
        from mcp.server.stdio import stdio_server
        server = self.server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    def http_app(self, host: str = "127.0.0.1"):
        """Streamable HTTP 앱. 세션 상태는 DB 에 있으므로 무상태(stateless)로 둔다"""
        app = self.server().streamable_http_app(stateless_http=True, json_response=True, host=host)
        return BearerAuth(app, self.auth)


def stdio_main(ontology: str, *, today: str | None = None, confirm: bool = False) -> None:
    token = os.getenv("MINIONTO_TOKEN")
    if not token:
        raise SystemExit("MINIONTO_TOKEN 환경 변수에 에이전트 토큰을 넣으세요 (python -m minionto token issue ...)")
    anyio.run(MiniOntoMCP(ontology, today=today, confirm=confirm, stdio_token=token).run_stdio)


__all__ = ["MiniOntoMCP", "BearerAuth", "TOOL_SCOPES", "stdio_main"]
