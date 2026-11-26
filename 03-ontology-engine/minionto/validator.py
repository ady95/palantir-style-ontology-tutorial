"""온톨로지 정의의 의미 검증 (참조 무결성)

메타모델(metamodel.py)이 '문법'을 검사한다면, 여기서는 '뜻이 통하는지'를 검사한다.
- 링크·Action·조건식이 가리키는 객체 타입, 속성, 링크, 파라미터, 함수가 실제로 있는가
- 비교 대상 경로가 단일 값인가 (여러 개로 갈라지는 경로는 some/none 을 써야 한다)
- enum 속성에 허용되지 않은 값을 쓰지 않았는가
- Action 이 새 객체를 만들 때 필수 속성을 모두 채우는가
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .metamodel import Action, Condition, Edit, Ontology, Parameter, Property
from .schema import PathError, Schema

ACTOR_FIELDS = {"id", "role", "type"}
SPECIAL_REFS = {"$now", "$today"}
# OntoObject(objects.py)가 엔진 기능에 쓰는 이름
RESERVED_NAMES = {"pk", "label", "object_type", "properties", "link", "to_dict"}


@dataclass
class Issue:
    level: str      # error | warning
    path: str       # 정의 안의 위치 (예: actions.assign_accountant.edits[0])
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper()}] {self.path}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def error(self, path: str, msg: str) -> None:
        self.issues.append(Issue("error", path, msg))

    def warning(self, path: str, msg: str) -> None:
        self.issues.append(Issue("warning", path, msg))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


class Validator:
    def __init__(self, onto: Ontology, base_dir: Path | None = None):
        self.onto = onto
        self.schema = Schema(onto)
        self.base_dir = base_dir
        self.r = Report()

    # ------------------------------------------------------------------ 진입점
    def run(self) -> Report:
        self._object_types()
        self._link_types()
        self._functions()
        for name, action in self.onto.actions.items():
            self._action(f"actions.{name}", action)
        return self.r

    # ------------------------------------------------------------------ Object Type
    def _object_types(self) -> None:
        for name, ot in self.onto.object_types.items():
            p = f"object_types.{name}"
            props = ot.properties
            if ot.source.primary_key not in props:
                self.r.error(f"{p}.source.primary_key", f"속성 '{ot.source.primary_key}' 이 없습니다")
            elif not props[ot.source.primary_key].required:
                self.r.warning(f"{p}.properties.{ot.source.primary_key}", "기본키 속성은 required: true 가 좋습니다")
            if ot.title_property not in props:
                self.r.error(f"{p}.title_property", f"속성 '{ot.title_property}' 이 없습니다")

            columns: dict[str, str] = {}
            for pname, prop in props.items():
                if pname in RESERVED_NAMES:
                    self.r.warning(f"{p}.properties.{pname}",
                                   f"'{pname}' 은 객체 API의 예약 이름입니다. obj[\"{pname}\"] 으로 읽어야 합니다")
                col = prop.column or pname
                if col in columns:
                    self.r.error(f"{p}.properties.{pname}", f"컬럼 '{col}' 을 '{columns[col]}' 과 함께 씁니다")
                columns[col] = pname

            for iname in ot.implements:
                iface = self.onto.interfaces.get(iname)
                if iface is None:
                    self.r.error(f"{p}.implements", f"인터페이스 '{iname}' 가 없습니다")
                    continue
                for ipname, ip in iface.properties.items():
                    if ipname not in props:
                        self.r.error(f"{p}.implements", f"'{iname}' 의 속성 '{ipname}' 이 없습니다")
                    elif not _compatible(props[ipname].type, ip.type):
                        self.r.error(f"{p}.properties.{ipname}",
                                     f"'{iname}' 은 {ip.type} 타입을 요구하지만 {props[ipname].type} 입니다")

    # ------------------------------------------------------------------ Link Type
    def _link_types(self) -> None:
        ots = self.onto.object_types
        for name, lt in self.onto.link_types.items():
            p = f"link_types.{name}"
            ok = True
            for side, ot_name in (("from", lt.from_), ("to", lt.to)):
                if ot_name not in ots:
                    self.r.error(f"{p}.{side}", f"객체 타입 '{ot_name}' 이 없습니다")
                    ok = False
            if not ok:
                continue
            fk = lt.from_key or ots[lt.from_].source.primary_key
            tk = lt.to_key or ots[lt.to].source.primary_key
            fprop, tprop = ots[lt.from_].properties.get(fk), ots[lt.to].properties.get(tk)
            if fprop is None:
                self.r.error(f"{p}.from_key", f"'{lt.from_}' 에 속성 '{fk}' 이 없습니다")
            if tprop is None:
                self.r.error(f"{p}.to_key", f"'{lt.to}' 에 속성 '{tk}' 이 없습니다")
            if fprop and tprop and not _compatible(fprop.type, tprop.type):
                self.r.error(p, f"키 타입이 다릅니다: {lt.from_}.{fk}({fprop.type}) ↔ {lt.to}.{tk}({tprop.type})")
            for side, ot_name, lname in (("forward_name", lt.from_, lt.forward_name),
                                         ("reverse_name", lt.to, lt.reverse_name)):
                if lname in ots[ot_name].properties:
                    self.r.error(f"{p}.{side}", f"'{ot_name}' 의 속성 이름 '{lname}' 과 겹칩니다")
        for ot_name, lname in self.schema.duplicates:
            self.r.error("link_types", f"'{ot_name}' 에서 링크 이름 '{lname}' 이 두 번 이상 쓰였습니다")

    # ------------------------------------------------------------------ Function
    def _functions(self) -> None:
        for name, fn in self.onto.functions.items():
            p = f"functions.{name}"
            for pname, param in fn.inputs.items():
                self._parameter(f"{p}.inputs.{pname}", param)
            self._parameter(f"{p}.returns", fn.returns)
            self._implementation(f"{p}.implementation", fn.implementation)

    def _implementation(self, path: str, impl: str) -> None:
        if self.base_dir is None:
            return
        module, func = impl.split(":")
        file = self.base_dir / (module.replace(".", "/") + ".py")
        if not file.exists():
            self.r.warning(path, f"구현 파일 {file.name} 이 아직 없습니다")
            return
        spec = importlib.util.spec_from_file_location(f"_minionto_impl_{module}", file)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 — 사용자 코드 오류를 그대로 보고
            self.r.error(path, f"{file.name} 을 불러오지 못했습니다: {e}")
            return
        if not callable(getattr(mod, func, None)):
            self.r.error(path, f"{file.name} 에 함수 '{func}' 가 없습니다")

    def _parameter(self, path: str, param: Parameter) -> None:
        if param.object_type and param.object_type not in self.onto.object_types:
            self.r.error(path, f"객체 타입 '{param.object_type}' 이 없습니다")
        for fname, f in (param.fields or {}).items():
            self._parameter(f"{path}.fields.{fname}", f)

    # ------------------------------------------------------------------ Action
    def _action(self, p: str, action: Action) -> None:
        if action.target not in self.onto.object_types:
            self.r.error(f"{p}.target", f"객체 타입 '{action.target}' 이 없습니다")
            return
        for pname, param in action.parameters.items():
            self._parameter(f"{p}.parameters.{pname}", param)

        roles = set(self.onto.roles)
        for role in action.permission.roles:
            if role not in roles:
                self.r.error(f"{p}.permission.roles", f"역할 '{role}' 이 roles 에 없습니다")
        if action.approval:
            for role in action.approval.approver_roles:
                if role not in roles:
                    self.r.error(f"{p}.approval.approver_roles", f"역할 '{role}' 이 roles 에 없습니다")

        for i, rule in enumerate(action.preconditions):
            self._condition(f"{p}.preconditions[{i}]", rule.check, action.target, action)
        if action.permission.check:
            self._condition(f"{p}.permission.check", action.permission.check, action.target, action)
        if action.approval:
            self._condition(f"{p}.approval.required_if", action.approval.required_if, action.target, action)
        for i, edit in enumerate(action.edits):
            self._edit(f"{p}.edits[{i}]", edit, action)

    def _edit(self, p: str, edit: Edit, action: Action) -> None:
        if edit.update is not None:
            self._assignments(f"{p}.set", edit.set, action.target, action, allow_pk=False)
        elif edit.update_linked is not None:
            end = self.schema.links[action.target].get(edit.update_linked)
            if end is None:
                self.r.error(f"{p}.update_linked", f"'{action.target}' 에 링크 '{edit.update_linked}' 가 없습니다")
                return
            if edit.where:
                self._condition(f"{p}.where", edit.where, end.target, action)
            self._assignments(f"{p}.set", edit.set, end.target, action, allow_pk=False)
        else:
            ot = self.onto.object_types.get(edit.create)
            if ot is None:
                self.r.error(f"{p}.create", f"객체 타입 '{edit.create}' 이 없습니다")
                return
            self._assignments(f"{p}.values", edit.values, edit.create, action, allow_pk=True)
            pk = ot.source.primary_key
            missing = [n for n, prop in ot.properties.items()
                       if prop.required and n not in edit.values
                       and not (n == pk and ot.source.id_strategy != "provided")]
            if missing:
                self.r.error(f"{p}.values", f"'{edit.create}' 의 필수 속성이 빠졌습니다: {', '.join(missing)}")

    def _assignments(self, p: str, values: dict[str, Any], obj: str, action: Action, allow_pk: bool) -> None:
        ot = self.onto.object_types[obj]
        for key, value in values.items():
            prop = ot.properties.get(key)
            if prop is None:
                self.r.error(f"{p}.{key}", f"'{obj}' 에 속성 '{key}' 이 없습니다")
                continue
            if key == ot.source.primary_key and not allow_pk:
                self.r.error(f"{p}.{key}", "기본키는 수정할 수 없습니다")
            self._value(f"{p}.{key}", value, prop, action)

    # ------------------------------------------------------------------ 조건식
    def _condition(self, p: str, c: Condition, obj: str, action: Action) -> None:
        if c.all_ is not None or c.any_ is not None:
            key = "all" if c.all_ is not None else "any"
            for i, sub in enumerate(c.all_ if c.all_ is not None else c.any_):
                self._condition(f"{p}.{key}[{i}]", sub, obj, action)
        elif c.not_ is not None:
            self._condition(f"{p}.not", c.not_, obj, action)
        elif c.some is not None or c.none is not None:
            q, key = (c.some, "some") if c.some is not None else (c.none, "none")
            end = self.schema.links[obj].get(q.link)
            if end is None:
                self.r.error(f"{p}.{key}.link", f"'{obj}' 에 링크 '{q.link}' 가 없습니다")
            elif q.where:
                self._condition(f"{p}.{key}.where", q.where, end.target, action)
        elif c.function is not None:
            fn = self.onto.functions.get(c.function)
            if fn is None:
                self.r.error(f"{p}.function", f"함수 '{c.function}' 가 없습니다")
                return
            if fn.returns.type != "boolean":
                self.r.error(f"{p}.function", f"조건에 쓰는 함수는 boolean 을 반환해야 합니다 ({fn.returns.type})")
            for arg in (c.args or {}):
                if arg not in fn.inputs:
                    self.r.error(f"{p}.args.{arg}", f"함수 '{c.function}' 에 입력 '{arg}' 가 없습니다")
            for iname, inp in fn.inputs.items():
                if inp.required and iname not in (c.args or {}):
                    self.r.error(f"{p}.args", f"함수 '{c.function}' 의 필수 입력 '{iname}' 이 빠졌습니다")
            for arg, v in (c.args or {}).items():
                self._ref(f"{p}.args.{arg}", v, action)
        else:
            prop = self._field(f"{p}.field", c.field, obj, action)
            if c.op in ("is_null", "not_null"):
                return
            values = c.value if isinstance(c.value, list) else [c.value]
            for v in values:
                self._value(f"{p}.value", v, prop, action)

    def _field(self, p: str, path: str, obj: str, action: Action) -> Property | None:
        """비교 왼쪽(field)을 해석해 속성 정의를 돌려준다. 참조($...)면 None."""
        if path.startswith("$"):
            self._ref(p, path, action)
            return self._param_as_property(path, action)
        try:
            res = self.schema.resolve(obj, path)
        except PathError as e:
            self.r.error(p, str(e))
            return None
        if res.prop is None:
            self.r.error(p, f"'{path}' 는 링크입니다. 비교에는 속성을 써야 합니다")
            return None
        if res.many:
            self.r.error(p, f"'{path}' 는 여러 객체로 갈라지는 경로입니다. some/none 을 쓰세요")
        return res.prop

    def _value(self, p: str, value: Any, prop: Property | None, action: Action) -> None:
        if isinstance(value, str) and value.startswith("$"):
            self._ref(p, value, action)
            return
        if prop is None:
            return
        if prop.type == "enum" and value not in prop.values:
            self.r.error(p, f"'{value}' 는 허용 값이 아닙니다 ({', '.join(prop.values)})")
        elif prop.type == "integer" and not isinstance(value, int):
            self.r.error(p, f"정수가 필요합니다: {value!r}")
        elif prop.type == "boolean" and not isinstance(value, bool):
            self.r.error(p, f"true/false 가 필요합니다: {value!r}")

    def _ref(self, p: str, value: Any, action: Action) -> None:
        if not (isinstance(value, str) and value.startswith("$")):
            return
        if value in SPECIAL_REFS:
            return
        head, _, rest = value[1:].partition(".")
        if head == "actor":
            if rest not in ACTOR_FIELDS:
                self.r.error(p, f"'{value}': $actor 에는 {', '.join(sorted(ACTOR_FIELDS))} 만 있습니다")
        elif head == "params":
            pname = rest.split(".")[0]
            if pname not in action.parameters:
                self.r.error(p, f"'{value}': 파라미터 '{pname}' 가 정의되지 않았습니다")
        elif head == "target":
            try:
                res = self.schema.resolve(action.target, rest)
                if res.many:
                    self.r.error(p, f"'{value}' 는 여러 객체로 갈라지는 경로입니다")
            except PathError as e:
                self.r.error(p, str(e))
        else:
            self.r.error(p, f"알 수 없는 참조 '{value}' ($actor·$params·$target·$now·$today 만 가능)")

    def _param_as_property(self, ref: str, action: Action) -> Property | None:
        """$params.x 가 enum 이면 값 검사를 위해 Property 처럼 다룬다."""
        if not ref.startswith("$params."):
            return None
        param = action.parameters.get(ref.split(".")[1])
        if param and param.type == "enum":
            return Property(type="enum", values=param.values)
        return None


def _compatible(a: str, b: str) -> bool:
    text = {"string", "enum"}
    number = {"decimal", "float"}
    return a == b or (a in text and b in text) or (a in number and b in number)


def validate(onto: Ontology, base_dir: Path | None = None) -> Report:
    return Validator(onto, base_dir).run()
