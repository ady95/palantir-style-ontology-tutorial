"""SDK Generator — 온톨로지 메타데이터로 타입이 있는 Python SDK 를 만든다 (19장)

    python -m minionto sdk ontologies/taxos/ontology.yaml -o build/taxos_sdk.py
    python -m minionto sdk --url http://localhost:8800 --ontology taxos --token ... -o build/taxos_sdk.py

생성 결과
- 객체 타입마다 dataclass (enum 속성은 Literal, 날짜·금액은 date·Decimal 로 변환)
- 클라이언트 클래스: 객체 타입 이름의 속성(client.TaxReturn), Function·Action 메서드
- Action 메서드의 독스트링에 권한·승인·실행 조건을 적어, 에디터와 AI 코딩 도구가 그대로 읽게 한다

서버가 나중에 속성을 추가해도 from_dict 는 모르는 키를 무시하므로 기존 SDK 가 깨지지 않는다
(Registry 의 'compatible' 변경). 속성 삭제·타입 변경은 'breaking' 이며 SDK 를 다시 만들어야 한다.
"""

from __future__ import annotations

import keyword
import re

PY_TYPES = {"string": "str", "integer": "int", "float": "float", "decimal": "Decimal", "boolean": "bool",
            "date": "date", "datetime": "datetime", "binary": "str", "enum": "str"}
CONVERTERS = {"date": "to_date", "datetime": "to_datetime", "decimal": "to_decimal"}


def _ident(name: str) -> str:
    name = re.sub(r"\W", "_", name)
    return name + "_" if keyword.iskeyword(name) or name in {"self"} else name


# RemoteOntology 의 메서드·속성과 겹치는 Function·Action 이름은 뒤에 _ 를 붙인다
CLIENT_RESERVED = {"get", "post", "call", "apply", "objects", "metadata", "close", "candidates", "approvals",
                   "decide", "linked", "get_object", "check_version", "link_target", "http", "prefix", "name",
                   "available", "history", "me",
                   "ontology", "version", "models", "link_targets"}


def _method(name: str) -> str:
    name = _ident(name)
    return name + "_" if name in CLIENT_RESERVED else name


def _doc(text: str, indent: str) -> str:
    text = text.replace('"""', "'''").strip()
    lines = text.splitlines() or [""]
    if len(lines) == 1:
        return f'{indent}"""{lines[0]}"""\n'
    return f'{indent}"""' + lines[0] + "\n" + "".join(f"{indent}{ln}\n" if ln else "\n" for ln in lines[1:]) \
        + f'{indent}"""\n'


def _prop_type(p: dict) -> str:
    if p["type"] == "enum" and p.get("values"):
        return "Literal[" + ", ".join(repr(v) for v in p["values"]) + "]"
    return PY_TYPES[p["type"]]


def _param_type(p: dict, meta: dict) -> str:
    t = p["type"]
    if t == "object":
        target = meta["object_types"][p["object_type"]]
        pk_type = PY_TYPES[target["properties"][target["primary_key"]]["type"]]
        return f"{pk_type} | {p['object_type']}"
    if t == "list":
        item = p.get("items") or "string"
        inner = _param_type({"type": item, "object_type": p.get("object_type")}, meta) \
            if item == "object" else PY_TYPES.get(item, "Any")
        return f"list[{inner}]"
    if t == "struct":
        return "dict[str, Any]"
    return _prop_type(p)


def _signature(params: dict, meta: dict) -> tuple[list[str], list[str]]:
    """(필수 인자, 선택 인자) — 선택 인자는 기본값 None"""
    req, opt = [], []
    for name, p in params.items():
        t = _param_type(p, meta)
        if p.get("required", True) and p.get("default") is None:
            req.append(f"{_ident(name)}: {t}")
        else:
            opt.append(f"{_ident(name)}: {t} | None = None")
    return req, opt


def _args_dict(params: dict) -> str:
    items = ", ".join(f"{name}=_pk({_ident(name)})" if p["type"] == "object" else f"{name}={_ident(name)}"
                      for name, p in params.items())
    return f"drop_none({items})"


def generate(meta: dict, *, class_name: str | None = None) -> str:
    name = meta["name"]
    cls = class_name or "".join(part.title() for part in name.split("_")) + "Client"
    out: list[str] = []
    w = out.append
    w(f'"""{meta.get("display_name") or name} SDK — 자동 생성 파일 (python -m minionto sdk). 직접 고치지 마세요.\n\n')
    w(f"온톨로지 {name} v{meta['version']}" + (f" · checksum {meta['checksum'][:12]}" if meta.get("checksum") else ""))
    w('\n"""\n\nfrom __future__ import annotations\n\n')
    w("from dataclasses import dataclass\nfrom datetime import date, datetime\nfrom decimal import Decimal\n")
    w("from typing import Any, ClassVar, Literal\n\n")
    w("from minionto.sdk import (ActionResponse, RemoteOntology, TypedObjectSet, drop_none, to_date,\n"
      "                          to_datetime, to_decimal)\n\n\n")
    w("def _pk(value: Any) -> Any:\n"
      "    \"\"\"객체 dataclass 를 넘기면 기본키로 바꾼다\"\"\"\n"
      "    return getattr(value, value.__pk__) if hasattr(value, '__pk__') else value\n\n\n")

    # ---------------------------------------------------------------- Object Types
    w("# " + "=" * 70 + " Object Types\n")
    for tname, ot in meta["object_types"].items():
        pk = ot["primary_key"]
        props = ot["properties"]
        w("@dataclass\nclass " + tname + ":\n")
        doc = f"{ot['display_name']} (기본키 {pk})"
        if ot.get("description"):
            doc += "\n\n" + ot["description"]
        if ot["links"]:
            doc += "\n\n링크: " + ", ".join(f"{k} → {v['target']}{'[]' if v['many'] else ''}"
                                         for k, v in ot["links"].items())
        if ot["actions"]:
            doc += "\nAction: " + ", ".join(ot["actions"])
        sm = ot.get("state_machine")
        if sm:
            doc += (f"\n\n상태({sm['property']}): 처음 {', '.join(sm['initial'])} · 종료 {', '.join(sm['terminal'])}\n"
                    + "\n".join(f"  {'·'.join(t['from'])} → {t['to']}  ({a})" for a, t in sm["transitions"].items()))
        w(_doc(doc, "    "))
        w(f"    __object_type__: ClassVar[str] = {tname!r}\n    __pk__: ClassVar[str] = {pk!r}\n\n")
        order = [pk] + [p for p in props if p != pk]
        for pname in order:
            p = props[pname]
            t = _prop_type(p)
            comment = f"  # {p['display_name']}" if p.get("display_name") else ""
            if pname == pk:
                w(f"    {_ident(pname)}: {t}{comment}\n")
            else:
                w(f"    {_ident(pname)}: {t} | None = None{comment}\n")
        w(f"\n    @classmethod\n    def from_dict(cls, d: dict) -> {tname}:\n")
        w("        \"\"\"응답 → 객체. 모르는 키(서버에 새로 생긴 속성)는 무시한다\"\"\"\n")
        w("        return cls(\n")
        for pname in order:
            conv = CONVERTERS.get(props[pname]["type"])
            value = f"d.get({pname!r})"
            w(f"            {_ident(pname)}={conv}({value}),\n" if conv else f"            {_ident(pname)}={value},\n")
        w("        )\n\n\n")

    # ---------------------------------------------------------------- Client
    w("# " + "=" * 70 + " Client\n")
    w(f"class {cls}(RemoteOntology):\n")
    w(_doc(f"{meta.get('display_name') or name} API 클라이언트\n\n"
           f"    client = {cls}('http://localhost:8800', token='...')\n"
           f"    client.check_version()   # 서버 버전과 호환되는지 확인", "    "))
    w(f"    ontology = {name!r}\n    version = {meta['version']!r}\n")
    w("    models: ClassVar[dict[str, type]] = {" +
      ", ".join(f"{t!r}: {t}" for t in meta["object_types"]) + "}\n")
    w("    link_targets: ClassVar[dict[str, dict[str, str]]] = {\n")
    for tname, ot in meta["object_types"].items():
        w(f"        {tname!r}: {{" + ", ".join(f"{k!r}: {v['target']!r}" for k, v in ot["links"].items()) + "},\n")
    w("    }\n\n")
    w("    def __init__(self, base_url: str, token: str | None = None, **kw: Any):\n"
      "        super().__init__(base_url, self.ontology, token, **kw)\n\n")

    w("    # ---- 객체 집합 (client.TaxReturn.where(...).all())\n")
    for tname, ot in meta["object_types"].items():
        w(f"    @property\n    def {tname}(self) -> TypedObjectSet[{tname}]:\n")
        w(_doc(ot["display_name"], "        "))
        w(f"        return TypedObjectSet(self, {tname!r})\n\n")

    if meta["functions"]:
        w("    # ---- Function\n")
    for fname, fn in meta["functions"].items():
        req, opt = _signature(fn["inputs"], meta)
        args = ", ".join(["self"] + req + (["*"] + opt if opt else []))
        ret = fn["returns"]
        rtype = "dict[str, Any]" if ret["type"] in ("struct", "object") else _param_type(ret, meta)
        if ret["type"] == "list" and ret.get("items") == "object":
            rtype = "list[dict[str, Any]]"
        w(f"    def {_method(fname)}({args}) -> {rtype}:\n")
        w(_doc(f"{fn.get('display_name') or fname} — {fn['description']}\n\n반환: {fn['returns']['type']}",
               "        "))
        call = f"self.call({fname!r}, **{_args_dict(fn['inputs'])})"
        conv = CONVERTERS.get(ret["type"])               # JSON 문자열 → Decimal·date
        w(f"        return {conv}({call})\n\n" if conv else f"        return {call}\n\n")

    if meta["actions"]:
        w("    # ---- Action (dry_run=True 면 검사·변경 결과만 보고 롤백)\n")
    for aname, act in meta["actions"].items():
        req, opt = _signature(act["parameters"], meta)
        target_type = _param_type({"type": "object", "object_type": act["target"]}, meta)
        args = ", ".join(["self", f"target: {target_type}"] + req + ["*"] + opt
                         + ["dry_run: bool = False", "expect: dict[str, Any] | None = None"])
        w(f"    def {_method(aname)}({args}) -> ActionResponse:\n")
        perm = act["permission"]
        doc = [f"{act['display_name']} (대상: {act['target']})"]
        if act.get("description"):
            doc += ["", act["description"].strip()]
        doc += ["", f"권한: {', '.join(perm['roles'])} ({', '.join(perm['actor_types'])})"]
        if act.get("approval"):
            doc.append(f"승인: {', '.join(act['approval']['approver_roles'])}"
                       + (f" — {act['approval']['reason']}" if act["approval"].get("reason") else ""))
        if act["preconditions"]:
            doc += ["실행 조건 (못 맞추면 rejected 와 함께 돌려주는 메시지):"] + [f"  - {m}" for m in act["preconditions"]]
        for t in act.get("transitions") or []:
            doc.append(f"상태 전이: {t['object_type']}.{t['property']} {'·'.join(t['from'])} → {t['to']}")
        if act["emits"]:
            doc.append(f"이벤트: {', '.join(act['emits'])}")
        w(_doc("\n".join(doc), "        "))
        w(f"        return self.apply({aname!r}, _pk(target), params={_args_dict(act['parameters'])}, "
          "dry_run=dry_run, expect=expect)\n\n")
    return "".join(out).rstrip() + "\n"


def metadata_from_file(path: str) -> dict:
    from .describe import public_metadata
    from .registry import Package
    pkg = Package.from_file(path)
    return public_metadata(pkg.onto, checksum=pkg.checksum)


def metadata_from_url(base_url: str, ontology: str, token: str) -> dict:
    import httpx
    r = httpx.get(f"{base_url.rstrip('/')}/v1/ontologies/{ontology}",
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()
