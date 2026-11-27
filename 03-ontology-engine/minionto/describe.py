"""공개 메타데이터 — API·SDK·AI 에이전트에게 보여 줄 온톨로지 설명 (19장)

정의 파일에는 테이블·컬럼·조인 키 같은 '물리 매핑'과 Function 구현 위치가 들어 있다.
외부 사용자는 업무 용어(객체·속성·링크·Function·Action)만 알면 되므로 그것만 내보낸다.
물리 매핑을 숨겨 두면 DB 구조를 바꿔도 API·SDK 사용자는 영향을 받지 않는다.
"""

from __future__ import annotations

from typing import Any

from .metamodel import Ontology, Parameter
from .schema import Schema
from .states import StateMachines


def _param(p: Parameter) -> dict[str, Any]:
    out: dict[str, Any] = {"type": p.type, "required": p.required}
    for key in ("description", "default", "values", "object_type", "items"):
        value = getattr(p, key)
        if value is not None:
            out[key] = value
    if p.fields:
        out["fields"] = {k: _param(v) for k, v in p.fields.items()}
    return out


def public_metadata(onto: Ontology, *, version: str | None = None, checksum: str | None = None) -> dict:
    schema = Schema(onto)
    states = StateMachines(onto)
    info = onto.ontology
    object_types = {}
    for name, ot in onto.object_types.items():
        object_types[name] = {
            "display_name": ot.display_name,
            "description": ot.description,
            "primary_key": ot.source.primary_key,
            "title_property": ot.title_property,
            "implements": ot.implements,
            "properties": {
                pname: {k: v for k, v in {
                    "type": p.type, "required": p.required, "display_name": p.display_name,
                    "description": p.description, "values": p.values}.items() if v not in (None, False)}
                for pname, p in ot.properties.items()},
            "links": {end.name: {"target": end.target, "many": end.many}
                      for end in schema.links[name].values()},
            "actions": [a for a, act in onto.actions.items() if act.target == name],
            "state_machine": ({"property": ot.state_machine.property, "initial": ot.state_machine.initial,
                               "terminal": ot.state_machine.terminal,
                               "transitions": {a: {"from": t.from_, "to": t.to}
                                               for a, t in ot.state_machine.transitions.items()}}
                              if ot.state_machine else None),
        }
    functions = {
        name: {"display_name": fn.display_name, "description": fn.description,
               "inputs": {k: _param(v) for k, v in fn.inputs.items()}, "returns": _param(fn.returns)}
        for name, fn in onto.functions.items()}
    actions = {
        name: {"display_name": act.display_name, "description": act.description, "target": act.target,
               "parameters": {k: _param(v) for k, v in act.parameters.items()},
               "preconditions": [r.message for r in act.preconditions],
               "permission": {"roles": act.permission.roles, "actor_types": act.permission.actor_types},
               "approval": ({"approver_roles": act.approval.approver_roles, "reason": act.approval.reason}
                            if act.approval else None),
               "emits": act.emits,
               "transitions": [{"object_type": r.object_type, "property": r.property,
                                "from": list(r.from_), "to": r.to} for r in states.rules_for_action(name)]}
        for name, act in onto.actions.items()}
    sec = onto.security
    security = {
        "row_policies": {n: {"description": p.description, "roles": p.roles, "actor_types": p.actor_types,
                             "object_types": sorted(p.rules)} for n, p in sec.row_policies.items()},
        "property_policies": {n: {"description": p.description, "roles": p.roles, "actor_types": p.actor_types,
                                  "hidden": p.hidden} for n, p in sec.property_policies.items()},
    }
    return {
        "name": info.name, "version": version or info.version, "checksum": checksum,
        "display_name": info.display_name, "description": info.description, "roles": onto.roles,
        "object_types": object_types, "functions": functions, "actions": actions,
        "security": security,             # 21장: 정책 이름·설명·대상 (조건식은 내보내지 않는다)
    }
