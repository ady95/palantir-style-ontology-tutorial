"""AI 에이전트 인터페이스 — 온톨로지를 LLM 이 부를 수 있는 도구로 내보낸다 (25장)

에이전트에게 주는 것은 두 가지다.

1. 컨텍스트 — 이 실행자에게 보이는 객체·속성·링크, 쓸 수 있는 Function, 실행할 수 있는 Action
   (실행 조건·승인 조건·상태 전이). 실행할 수 없는 Action 과 감춘 속성은 처음부터 빼서 토큰도 아낀다.
2. 도구 — 조회(find_objects·get_object·call_function), 판단(list_actions·check_action),
   실행(run_action). 모든 도구는 예외를 던지지 않고 JSON 결과를 돌려주며, 결과마다
   next_step(다음에 무엇을 할지)을 붙인다.

실행 도구에는 세 가지 장치가 있다.
- 읽은 값으로 expect: 에이전트가 이 세션에서 읽은 상태를 expect 로 보낸다 (20장). 읽지 않은 대상은
  실행하지 않는다 (read_first).
- 업무 단위 멱등 키: agent:<업무 ID>:<Action>:<대상>. 같은 업무에서 같은 실행을 다시 보내면 처음
  결과를 돌려준다 (22장). 아무것도 바꾸지 않은 결과(rejected 등) 뒤에는 새 키로 다시 시도할 수 있다.
- 확인 모드: confirm=True 이면 run_action 은 dry-run 미리보기만 돌려주고, 실제 실행은 사람이
  session.confirm(token) 으로 한다. 에이전트의 도구 목록에는 확인 도구가 없다.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .actions import ActionCheck, ActionResult
from .functions import FunctionError
from .metamodel import Parameter
from .objects import OntologyClient, OntoObject
from .policy import PolicyError
from .query import QueryError

OPS = ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "not_null"]

# 결과 상태별 다음 행동 — 에이전트가 실패 사유로 다음 행동을 고르게 한다
NEXT = {
    "ready": "run_action 으로 실행할 수 있습니다.",
    "preview": "미리보기입니다. 아무것도 바뀌지 않았습니다. 실행하려면 dry_run 없이 다시 호출하세요.",
    "completed": "완료되었습니다. 결과를 사용자에게 알리세요.",
    "replayed": "이 업무에서 이미 실행한 요청입니다. 다시 실행되지 않았습니다. 처음 결과를 그대로 전하세요.",
    "approval_required": "승인이 필요합니다. 같은 요청을 반복하지 말고, 승인을 기다린다고 사용자에게 알리세요.",
    "needs_input": "missing 에 있는 입력을 사용자에게 물어보세요. 추측해서 채우지 마세요.",
    "rejected": "업무 규칙상 지금은 할 수 없습니다. 같은 요청을 반복하지 말고 사유를 사용자에게 전하세요.",
    "stale": "대상이 그사이 바뀌었습니다. get_object 로 다시 읽고 판단하세요.",
    "denied": "이 실행자에게 권한이 없습니다. 우회하지 말고, 담당자에게 맡기도록 사용자에게 안내하세요.",
    "invalid": "입력이 잘못되었습니다. messages 를 보고 인자를 고쳐 다시 호출하세요.",
    "read_first": "get_object 로 대상을 먼저 읽으세요. 읽은 상태를 근거로 실행합니다.",
    "retry": "일시적 오류입니다. 같은 호출을 다시 하면 됩니다 (멱등 키로 중복 실행되지 않습니다).",
    "failed": "다시 시도해도 같은 결과입니다. 사용자에게 오류를 알리세요.",
    "confirmation_required": "사용자의 확인을 기다립니다. 미리보기(changes)를 사용자에게 보여 주고 멈추세요.",
    "budget_exceeded": "도구 호출 한도를 넘었습니다. 지금까지의 결과로 답하세요.",
    "ok": None,
}
# check_action 에서는 같은 상태라도 다음 행동이 다르다 — 아직 아무것도 하지 않았다
NEXT_CHECK = {
    "approval_required": "실행하면 바로 바뀌지 않고 승인 요청이 만들어집니다. 사용자가 요청한 일이면 "
                         "run_action 으로 승인 요청을 올리고, 승인 대기 중임을 알리세요.",
}


def _plain(value: Any) -> Any:
    """JSON 으로 보낼 수 있는 값으로 (객체는 타입·키·속성으로)"""
    if isinstance(value, OntoObject):
        return {"$type": value.object_type, "$pk": value.pk,
                **{k: _plain(v) for k, v in value.properties.items()}}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (date, datetime, Decimal, uuid.UUID)):
        return str(value)
    return value


def _param_schema(p: Parameter) -> dict:
    """정의 파일의 파라미터 → JSON Schema"""
    t = {"string": "string", "integer": "integer", "decimal": "number", "float": "number",
         "boolean": "boolean", "date": "string", "datetime": "string", "enum": "string",
         "object": "string"}.get(p.type, "string")
    s: dict[str, Any] = {"type": t}
    if p.type == "list":
        s = {"type": "array", "items": {"type": "string"}}
    if p.values:
        s["enum"] = list(p.values)
    desc = p.description or ""
    if p.type == "date":
        desc = (desc + " (YYYY-MM-DD)").strip()
    if p.type == "object" and p.object_type:
        desc = (desc + f" ({p.object_type} 의 키)").strip()
    if desc:
        s["description"] = desc
    return s


class AgentSession:
    """한 실행자(에이전트)의 한 업무(task) — 도구 목록, 도구 실행, 읽은 상태, 멱등 키를 가진다"""

    def __init__(self, client: OntologyClient, actor: dict, *, task_id: str | None = None,
                 max_rows: int = 20, max_calls: int = 30, confirm: bool = False):
        if actor.get("type") != "agent":
            raise ValueError("AgentSession 은 actor_type 이 agent 인 실행자용입니다")
        self.client, self.actor = client, dict(actor)
        self.task_id = task_id or uuid.uuid4().hex[:8]
        self.max_rows, self.max_calls, self.confirm_mode = max_rows, max_calls, confirm
        self.seen: dict[tuple[str, str], dict] = {}           # (타입, 키) → 읽은 상태 {속성: 값}
        self._keys: dict[tuple[str, str], dict] = {}          # (Action, 대상) → {key, expect, n, status}
        self._pending: dict[str, dict] = {}                   # 확인 대기 token → 요청
        self.calls: list[dict] = []                           # 도구 호출 기록 (관찰용)
        self.confirmations: list[dict] = []                   # 사람의 확인 기록 (26장)

    # ================================================================== 상태 저장 (26장)
    def export_state(self) -> dict:
        """세션 상태를 JSON 으로 — 프로세스가 바뀌어도(MCP HTTP 서버) 업무를 이어 가기 위해"""
        return {"task_id": self.task_id,
                "seen": [[t, pk, st] for (t, pk), st in self.seen.items()],
                "keys": [[a, tgt, slot] for (a, tgt), slot in self._keys.items()],
                "pending": self._pending, "calls": self.calls, "confirmations": self.confirmations}

    def load_state(self, state: dict) -> AgentSession:
        self.task_id = state.get("task_id", self.task_id)
        self.seen = {(t, pk): st for t, pk, st in state.get("seen", [])}
        self._keys = {(a, tgt): slot for a, tgt, slot in state.get("keys", [])}
        self._pending = dict(state.get("pending", {}))
        self.calls = list(state.get("calls", []))
        self.confirmations = list(state.get("confirmations", []))
        return self

    # ================================================================== 컨텍스트
    @property
    def onto(self):
        return self.client.onto

    def permitted_actions(self) -> list[str]:
        """이 실행자의 역할·유형으로 실행할 수 있는 Action (권한 조건·실행 조건은 대상마다 다르므로 제외)"""
        return [n for n, a in self.onto.actions.items()
                if self.actor["role"] in a.permission.roles and self.actor["type"] in a.permission.actor_types]

    def context(self) -> dict:
        """이 실행자에게 필요한 만큼만 — 감춘 속성, 실행할 수 없는 Action, 물리 매핑은 뺀다"""
        policy = self.client.policy
        types = {}
        for name, ot in self.onto.object_types.items():
            hidden = policy.hidden(name, self.actor)
            props = {}
            for pname, p in ot.properties.items():
                if pname in hidden:
                    continue
                props[pname] = p.type + (f"[{'|'.join(p.values)}]" if p.values else "")
            entry: dict[str, Any] = {"display_name": ot.display_name, "key": ot.source.primary_key,
                                     "properties": props,
                                     "links": {e.name: e.target + ("[]" if e.many else "")
                                               for e in self.client.schema.links[name].values()}}
            if ot.description:
                entry["description"] = ot.description
            if hidden:
                entry["hidden"] = sorted(hidden)
            if ot.state_machine:
                sm = ot.state_machine
                entry["states"] = {a: f"{'|'.join(t.from_)}→{t.to}" for a, t in sm.transitions.items()}
            types[name] = entry
        functions = {n: {"description": f.description or f.display_name,
                         "inputs": {k: (v.object_type or v.type) for k, v in f.inputs.items()}}
                     for n, f in self.onto.functions.items()}
        actions = {}
        for n in self.permitted_actions():
            a = self.onto.actions[n]
            rule = self.client.actions.states.rule(a.target, n)
            entry = {"display_name": a.display_name, "target": a.target,
                     "parameters": {k: _param_schema(p) | {"required": p.required and p.default is None}
                                    for k, p in a.parameters.items()},
                     "conditions": [r.message for r in a.preconditions]}
            if a.description:
                entry["description"] = a.description
            if rule:
                entry["transition"] = f"{rule.property}: {'|'.join(rule.from_)}→{rule.to}"
            if a.approval:
                entry["approval"] = a.approval.reason or "승인 필요"
            actions[n] = entry
        return {"actor": self.actor, "today": str(self.client.today or date.today()),
                "policies": {k: v for k, v in policy.describe_for(self.actor).items() if k != "hidden_properties"},
                "object_types": types, "functions": functions, "actions": actions}

    def context_text(self) -> str:
        """context() 를 시스템 프롬프트용 짧은 텍스트로"""
        c = self.context()
        a = c["actor"]
        lines = [f"실행자 {a['id']} (역할 {a['role']}, 유형 {a['type']}) · 기준일 {c['today']}"]
        for n, d in {**c["policies"]["row_policies"], **c["policies"]["property_policies"]}.items():
            lines.append(f"정책 {n}: {d}")
        lines.append("# 객체 (속성: 타입, 링크: 대상[]=여러 개)")
        for n, t in c["object_types"].items():
            lines.append(f"{n} {t['display_name']} — 키 {t['key']}" +
                         (f", 볼 수 없는 속성 {','.join(t['hidden'])}" if t.get("hidden") else ""))
            lines.append("  속성 " + ", ".join(f"{k}:{v}" for k, v in t["properties"].items()))
            if t["links"]:
                lines.append("  링크 " + ", ".join(f"{k}→{v}" for k, v in t["links"].items()))
            if t.get("states"):
                lines.append("  상태 전이 " + ", ".join(f"{k} {v}" for k, v in t["states"].items()))
        lines.append("# Function")
        for n, f in c["functions"].items():
            ins = ", ".join(f"{k}:{v}" for k, v in f["inputs"].items())
            lines.append(f"{n}({ins}) — {f['description']}")
        lines.append("# 실행할 수 있는 Action (목록에 없는 Action 은 이 실행자가 할 수 없다)")
        for n, x in c["actions"].items():
            ps = ", ".join(f"{k}{'' if p['required'] else '?'}:{p['type']}" for k, p in x["parameters"].items())
            lines.append(f"{n} {x['display_name']} — 대상 {x['target']}" + (f", 입력 {ps}" if ps else ""))
            if x.get("transition"):
                lines.append(f"  상태 {x['transition']}")
            for m in x["conditions"]:
                lines.append(f"  조건 {m}")
            if x.get("approval"):
                lines.append(f"  승인 {x['approval']}")
        return "\n".join(lines)

    # ================================================================== 도구 목록
    def tools(self, style: str = "generic") -> list[dict]:
        """OpenAI 호환 tool 정의. generic: 도구 6개 / per_action: Action 마다 도구 하나 (조회 도구는 같다)"""
        types = list(self.onto.object_types)
        actions = self.permitted_actions()
        target = {"type": "string", "description": "대상 객체의 키"}
        out = [
            self._tool("find_objects", "객체를 조건으로 찾는다. 결과는 최대 "
                       f"{self.max_rows}건이며 count 는 전체 건수다.", {
                           "object_type": {"type": "string", "enum": types},
                           "where": {"type": "array", "description": "모두 만족 (AND). 항목은 {field, op, value} — "
                                     "field 는 속성 또는 하나로 이어지는 링크.속성 (예: customer.risk_level). "
                                     "여러 개로 이어지는 링크는 {some: {link, where}} 또는 {none: {link, where}} "
                                     "(예: {some: {link: tax_returns, where: {field: status, op: eq, value: 자료수집중}}})",
                                     "items": {"type": "object", "properties": {
                                         "field": {"type": "string"}, "op": {"type": "string", "enum": OPS},
                                         "value": {}, "some": {"type": "object"}, "none": {"type": "object"}}}},
                           "order_by": {"type": "string", "description": "정렬 속성, 내림차순은 앞에 -"},
                           "limit": {"type": "integer"}}, ["object_type"]),
            self._tool("get_object", "객체 하나의 속성, 상태, 링크(하나면 키, 여럿이면 건수)를 읽는다.",
                       {"object_type": {"type": "string", "enum": types}, "id": target}, ["object_type", "id"]),
            self._tool("call_function", "Function 을 호출한다. 객체 입력에는 키를 넣는다.", {
                "name": {"type": "string", "enum": list(self.onto.functions)},
                "args": {"type": "object"}}, ["name", "args"]),
            self._tool("list_actions", "이 객체에 지금 할 수 있는 Action 과 할 수 없는 이유.",
                       {"object_type": {"type": "string", "enum": types}, "id": target}, ["object_type", "id"]),
        ]
        if style == "per_action":
            for n in actions:
                a = self.onto.actions[n]
                props = {"target": {"type": "string", "description": f"대상 {a.target} 의 키"}}
                props |= {k: _param_schema(p) for k, p in a.parameters.items()}
                props["dry_run"] = {"type": "boolean", "description": "true 면 바꾸지 않고 결과만 미리 본다"}
                desc = " ".join([a.display_name + ".", a.description or ""] +
                                [f"조건: {r.message}" for r in a.preconditions] +
                                ([f"승인: {a.approval.reason}"] if a.approval and a.approval.reason else []))
                req = ["target"] + [k for k, p in a.parameters.items() if p.required and p.default is None]
                out.append(self._tool(n, desc.strip(), props, req))
            return out
        out += [
            self._tool("check_action", "Action 을 실행하면 어떻게 될지 판단만 한다 (바꾸지 않음).", {
                "action": {"type": "string", "enum": actions}, "target": target,
                "params": {"type": "object"}}, ["action", "target"]),
            self._tool("run_action", "Action 을 실행한다. 먼저 get_object 로 대상을 읽어야 한다.", {
                "action": {"type": "string", "enum": actions}, "target": target,
                "params": {"type": "object"},
                "dry_run": {"type": "boolean", "description": "true 면 바꾸지 않고 결과만 미리 본다"}},
                ["action", "target"]),
        ]
        return out

    @staticmethod
    def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}}

    # ================================================================== 도구 실행
    def call(self, name: str, args: dict | str | None = None) -> dict:
        """도구 하나를 실행한다. 예외를 던지지 않는다 — 결과에 status·messages·next_step 이 있다"""
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError as e:
                return self._record(name, {}, self._out("invalid", [f"인자가 JSON 이 아닙니다: {e}"]))
        args = dict(args or {})
        if len(self.calls) >= self.max_calls:
            return self._record(name, args, self._out("budget_exceeded", [f"한도 {self.max_calls}회"]))
        handler = getattr(self, f"_t_{name}", None)
        if handler is None and name in self.permitted_actions():        # per_action 스타일
            dry = bool(args.pop("dry_run", False))
            target = args.pop("target", None)
            handler, args = self._t_run_action, {"action": name, "target": target, "params": args,
                                                 "dry_run": dry}
        if handler is None:
            out = self._out("invalid", [f"도구 '{name}' 이 없습니다"])
        else:
            try:
                out = handler(**args)
            except TypeError as e:
                out = self._out("invalid", [f"인자 오류: {e}"])
            except PolicyError as e:
                out = self._out("denied", [str(e)])
            except (QueryError, FunctionError) as e:
                out = self._out("invalid", [str(e)])
        return self._record(name, args, out)

    def _record(self, name: str, args: dict, out: dict) -> dict:
        self.calls.append({"tool": name, "args": args, "status": out["status"]})
        return out

    @staticmethod
    def _out(status: str, messages: list[str] | None = None, *, hints: dict | None = None, **data: Any) -> dict:
        out = {"status": status}
        if messages:
            out["messages"] = messages
        out |= {k: _plain(v) for k, v in data.items() if v not in (None, [], {})}
        step = (hints or {}).get(status) or NEXT.get(status)
        if step:
            out["next_step"] = step
        return out

    def _remember(self, otype: str, props: dict) -> None:
        """읽은 상태를 기억한다 — run_action 의 expect 가 된다"""
        ot = self.onto.object_types[otype]
        pk = props.get(ot.source.primary_key)
        if pk is None:
            return
        state = {}
        if ot.state_machine and ot.state_machine.property in props:
            state[ot.state_machine.property] = props[ot.state_machine.property]
        self.seen[(otype, str(pk))] = state

    # ------------------------------------------------------------------ 조회
    def _t_find_objects(self, object_type: str, where: list | None = None, order_by: str | None = None,
                        limit: int | None = None) -> dict:
        q = self.client.objects(object_type)
        if where:
            q = q.where({"all": [dict(w) for w in where]})
        q = q.secure(self.actor)
        total = q.count()
        if order_by:
            q = q.order_by(order_by)
        else:
            q = q.order_by(self.onto.object_types[object_type].source.primary_key)
        n = max(1, min(int(limit or self.max_rows), self.max_rows))
        rows = [o.properties for o in q.limit(n).all()]
        for r in rows:
            self._remember(object_type, r)
        return self._out("ok", count=total, returned=len(rows), rows=rows,
                         note=(f"rows 는 앞의 {len(rows)}건뿐입니다. 전체 건수는 count({total})이니 직접 세지 마세요. "
                               "더 필요하면 조건을 좁히세요." if total > len(rows) else None))

    def _t_get_object(self, object_type: str, id: str) -> dict:          # noqa: A002 — 도구 인자 이름
        obj = self.client.get(object_type, id, actor=self.actor)
        if obj is None:
            return self._out("invalid", [f"{object_type} '{id}' 가 없거나 볼 수 없습니다"])
        links = {}
        for end in self.client.schema.links[object_type].values():
            ls = obj.link(end.name).secure(self.actor)
            if end.many:
                links[end.name] = {"count": ls.count()}
            else:
                first = ls.first()
                links[end.name] = first.pk if first else None
        self._remember(object_type, obj.properties)
        return self._out("ok", object=obj.properties, hidden=sorted(obj.restricted) or None, links=links)

    def _t_call_function(self, name: str, args: dict | None = None) -> dict:
        return self._out("ok", result=self.client.functions.call_as(self.actor, name, **(args or {})))

    # ------------------------------------------------------------------ 판단
    def _t_list_actions(self, object_type: str, id: str) -> dict:         # noqa: A002
        av = self.client.actions.available(object_type, id, actor=self.actor)
        obj = self.client.get(object_type, id, actor=self.actor)
        if obj is not None:
            self._remember(object_type, obj.properties)
        rows = [{"action": c.action, "status": c.status, **({"why": c.messages} if c.status != "ready" else {})}
                for c in av.actions if c.status != "denied"]          # 권한 밖 Action 은 보여 주지 않는다
        return self._out("ok", state=(av.state or {}).get("value"), actions=rows)

    def _t_check_action(self, action: str, target: str, params: dict | None = None) -> dict:
        if action not in self.permitted_actions():
            return self._out("denied", [f"'{action}' 은 이 실행자가 할 수 없는 Action 입니다"])
        c: ActionCheck = self.client.actions.check(action, target, actor=self.actor, params=params or {})
        return self._out(c.status, c.messages, hints=NEXT_CHECK, next_state=c.next_state, missing=c.missing)

    # ------------------------------------------------------------------ 실행
    def _t_run_action(self, action: str, target: str, params: dict | None = None, dry_run: bool = False) -> dict:
        if action not in self.permitted_actions():
            return self._out("denied", [f"'{action}' 은 이 실행자가 할 수 없는 Action 입니다"])
        otype = self.onto.actions[action].target
        seen = self.seen.get((otype, str(target)))
        if seen is None:
            return self._out("read_first", [f"이 업무에서 {otype} '{target}' 를 읽은 적이 없습니다"])
        params = dict(params or {})
        if dry_run:
            return self._result(self.client.actions.apply(action, target, actor=self.actor, params=params,
                                                          dry_run=True, expect=seen or None))
        if self.confirm_mode:
            preview = self.client.actions.apply(action, target, actor=self.actor, params=params,
                                                dry_run=True, expect=seen or None)
            if preview.status != "completed":
                return self._result(preview)
            req = {"action": action, "target": target, "params": params}
            token = next((t for t, r in self._pending.items() if r == req), None) or uuid.uuid4().hex[:8]
            self._pending[token] = req                     # 같은 요청을 다시 보내면 같은 토큰
            return self._out("confirmation_required", [f"확인 토큰 {token}"], token=token,
                             changes=[self._change(c) for c in preview.changes])
        return self._execute(action, target, params)

    def _execute(self, action: str, target: str, params: dict) -> dict:
        otype = self.onto.actions[action].target
        slot = self._keys.get((action, str(target)))
        if slot is None or slot["status"] in ("rejected", "denied", "invalid", "failed"):
            n = 1 if slot is None else slot["n"] + 1           # 아무것도 바꾸지 않은 결과 뒤에는 새 키로
            slot = {"key": f"agent:{self.task_id}:{action}:{target}" + (f":{n}" if n > 1 else ""),
                    "expect": self.seen.get((otype, str(target))) or None, "n": n}
            self._keys[(action, str(target))] = slot
        res = self.client.actions.apply(action, target, actor=self.actor, params=params,
                                        expect=slot["expect"], idempotency_key=slot["key"])
        slot["status"] = "retry" if (res.status == "failed" and res.retryable) else res.status
        if res.ok:                                           # 바뀐 상태를 기억한다
            for t in res.transitions:
                key = (t.get("object_type"), str(t.get("pk")))
                if key in self.seen:
                    self.seen[key] = {t["property"]: t["to"]}
        return self._result(res, key=slot["key"])

    def confirm(self, token: str, approve: bool = True, by: dict | None = None) -> dict:
        """사람이 미리보기를 보고 확인한다 — 에이전트의 도구가 아니다. by: 확인한 사람 (기록용)"""
        req = self._pending.pop(token, None)
        if req is None:
            return self._out("invalid", [f"확인 대기 중인 토큰 '{token}' 이 없습니다"])
        out = (self._execute(req["action"], req["target"], req["params"]) if approve
               else self._out("rejected", ["사용자가 실행을 거절했습니다"]))
        self.confirmations.append({"token": token, **req, "approve": approve, "by": by,
                                   "at": str(self.client.now()), "status": out["status"],
                                   "execution_id": out.get("execution_id")})
        return out

    @staticmethod
    def _change(c: dict) -> dict:
        after = {k: v for k, v in c.get("after", {}).items() if k != "created_at"}
        before = c.get("before") or {}
        def f(v: Any) -> str:
            return "-" if v is None else str(v)
        diff = {k: f"{f(before.get(k))}→{f(v)}" for k, v in after.items() if before and before.get(k) != v}
        return {"op": c["op"], "object_type": c["object_type"], "pk": c["pk"], **({"changed": diff} if diff
                                                                                  else {"values": after})}

    def _result(self, res: ActionResult, key: str | None = None) -> dict:
        status = res.status
        if res.replayed:
            status = "replayed"
        elif res.dry_run and status == "completed":
            status = "preview"
        elif status == "failed" and res.retryable:
            status = "retry"
        elif status == "rejected" and any("그사이 바뀌었습니다" in m for m in res.messages):
            status = "stale"
        return self._out(status, res.messages, original_status=res.status if status == "replayed" else None,
                         dry_run=res.dry_run or None, changes=[self._change(c) for c in res.changes],
                         events=res.events, approval_id=res.approval_id,
                         execution_id=None if res.dry_run else res.execution_id, idempotency_key=key)


class SessionStore:
    """에이전트 세션 상태를 DB 에 둔다 (26장) — 세션 키 하나 = 업무 하나

    MCP 서버는 세션 키로 토큰 ID(jti)를 쓴다. 토큰 하나를 업무 하나에 발급하면, 같은 토큰으로 들어온
    요청은 연결이 바뀌어도 같은 읽은 상태·멱등 키·확인 대기를 이어 쓴다.
    """
    DDL = f"""CREATE SCHEMA IF NOT EXISTS minionto;
CREATE TABLE IF NOT EXISTS minionto.agent_sessions (
    session_key text PRIMARY KEY,
    actor       jsonb NOT NULL,
    state       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
)"""

    def __init__(self, client: OntologyClient):
        self.client = client
        for stmt in self.DDL.split(";"):
            client.conn.execute(stmt)

    def open(self, key: str, actor: dict, **kw: Any) -> AgentSession:
        """키의 세션을 불러온다 (없으면 새로). 실행자가 다르면 거부 — 남의 세션을 이어 쓰지 못하게"""
        row = self.client.conn.execute("SELECT actor, state FROM minionto.agent_sessions WHERE session_key = %s",
                                       (key,)).fetchone()
        s = AgentSession(self.client, actor, task_id=key, **kw)
        if row is None:
            return s
        if row["actor"].get("id") != actor["id"]:
            raise PolicyError(f"세션 '{key}' 은 다른 실행자의 세션입니다")
        return s.load_state(row["state"])

    def save(self, key: str, s: AgentSession) -> None:
        actor = {k: v for k, v in s.actor.items() if k in ("id", "role", "type")}
        self.client.conn.execute(
            """INSERT INTO minionto.agent_sessions (session_key, actor, state) VALUES (%s, %s, %s)
               ON CONFLICT (session_key) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""",
            (key, json.dumps(actor, ensure_ascii=False), json.dumps(s.export_state(), ensure_ascii=False,
                                                                     default=str)))

    def pending(self) -> list[dict]:
        """사람의 확인을 기다리는 실행 (모든 세션)"""
        rows = self.client.conn.execute(
            "SELECT session_key, actor, state FROM minionto.agent_sessions "
            "WHERE state->'pending' <> '{}'::jsonb ORDER BY updated_at").fetchall()
        return [{"session": r["session_key"], "actor": r["actor"]["id"], "token": t, **req}
                for r in rows for t, req in r["state"]["pending"].items()]


__all__ = ["AgentSession", "SessionStore"]
