"""OpenAI 호환 Chat Completions API 로 도구 호출 루프를 돈다 (25장)

    OPENAI_BASE_URL · OPENAI_API_KEY · OPENAI_MODEL (.env)

LLM 은 도구를 직접 실행하지 않는다. "이 도구를 이 인자로 불러 달라"고 요청할 뿐이고, 실제 실행은
AgentSession 이 실행자의 권한으로 한다. 루프는 최대 단계 수를 넘으면 멈춘다.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from .agent import AgentSession

SYSTEM_PROMPT = """너는 세무사무소의 업무 보조 AI 에이전트다. 온톨로지 도구로만 일한다.
- 데이터는 도구로 확인하고 추측하지 않는다. 도구 결과에 없는 값을 지어내지 않는다.
- 무엇을 바꾸기 전에는 get_object 로 대상을 읽고, check_action 이나 list_actions 로 지금 할 수 있는지 확인한다.
- 도구 결과의 next_step 을 따른다. 권한이 없거나 규칙상 안 되는 일은 우회하지 말고 이유를 설명한다.
- 사용자에게는 한국어로 짧게, 무엇을 했고 무엇이 남았는지 답한다.

# 온톨로지
"""


class LLMError(RuntimeError):
    pass


class ChatClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float = 120):
        from dotenv import load_dotenv
        load_dotenv()
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY 가 없습니다 (.env)")
        self.http = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, retries: int = 2, **kw: Any) -> dict:
        body = {"model": self.model, "messages": messages, **kw}
        if tools:
            body["tools"] = tools
        for attempt in range(retries + 1):
            r = self.http.post(f"{self.base_url}/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code < 500 or attempt == retries:
                break
            time.sleep(2 * (attempt + 1))                       # 일시적 서버 오류만 다시 시도
        if r.status_code != 200:
            raise LLMError(f"LLM API {r.status_code}: {r.text[:300]}")
        return r.json()

    def count_tokens(self, text: str) -> int:
        """시스템 프롬프트로 보냈을 때 늘어나는 입력 토큰 수 (API 의 usage 로 잰다)"""
        def prompt_tokens(system: str) -> int:
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": "."}]
            return self.chat(msgs, max_tokens=16)["usage"]["prompt_tokens"]
        return prompt_tokens(text) - prompt_tokens("")

    def close(self) -> None:
        self.http.close()


def run_agent(chat: ChatClient, session: AgentSession, request: str, *, style: str = "generic",
              max_steps: int = 10, on_step=None) -> dict:
    """사용자 요청 하나를 처리한다 → {answer, trace, steps, usage, stopped}"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT + session.context_text()},
                {"role": "user", "content": request}]
    tools = session.tools(style)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    trace: list[dict] = []
    for step in range(1, max_steps + 1):
        resp = chat.chat(messages, tools)
        for k in usage:
            usage[k] += resp.get("usage", {}).get(k, 0)
        msg = resp["choices"][0]["message"]
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"answer": msg.get("content"), "trace": trace, "steps": step, "usage": usage, "stopped": None}
        for tc in calls:
            name, raw = tc["function"]["name"], tc["function"].get("arguments") or "{}"
            out = session.call(name, raw)
            item = {"step": step, "tool": name, "args": raw, "result": out}
            trace.append(item)
            if on_step:
                on_step(item)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(out, ensure_ascii=False)})
    return {"answer": None, "trace": trace, "steps": max_steps, "usage": usage, "stopped": "max_steps"}


__all__ = ["ChatClient", "LLMError", "SYSTEM_PROMPT", "run_agent"]
