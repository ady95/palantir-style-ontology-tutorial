"""Function Engine — 온톨로지에 선언한 Function 을 찾아 실행하고 입력·출력을 검사한다

YAML 에는 '무엇을 받아 무엇을 돌려주는가'(계약)만 적고, 계산은 Python 으로 구현한다.

    functions:
      has_capacity:
        inputs:  { accountant: { type: object, object_type: Accountant } }
        returns: { type: boolean }
        implementation: functions:has_capacity       # ontologies/taxos/functions.py

    # ontologies/taxos/functions.py
    def has_capacity(ctx, accountant):
        return accountant.customers.count() < accountant.max_workload

엔진이 하는 일
- 입력 변환: object 파라미터에 기본키("A002")를 넘겨도 OntoObject 로 바꿔서 전달
- 입력 검사: 빠진 필수 입력, 정의에 없는 입력, enum 허용 값
- 출력 검사: 선언한 returns 타입과 맞는지 (boolean 이라 했는데 None 을 돌려주면 오류)
- 사람·Action·AI 에이전트가 모두 같은 구현을 호출한다
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .metamodel import Parameter
from .query import QueryError, coerce

if TYPE_CHECKING:
    from .objects import ObjectSet, OntologyClient, OntoObject


class FunctionError(RuntimeError):
    """Function 호출 오류 (정의 위반 또는 구현 오류)"""


@dataclass
class FunctionContext:
    """구현 함수의 첫 번째 인자. 온톨로지 조회와 다른 Function 호출을 제공한다."""
    client: OntologyClient

    @property
    def today(self) -> date:
        return self.client.today or date.today()

    def objects(self, object_type: str) -> ObjectSet:
        return self.client.objects(object_type)

    def get(self, object_type: str, pk: Any) -> OntoObject | None:
        return self.client.get(object_type, pk)

    def call(self, name: str, **args: Any) -> Any:
        return self.client.functions.call(name, **args)

    def sql(self, query: str, params: tuple | dict | None = None) -> list[dict]:
        """온톨로지로 표현하지 못하는 계산을 위한 탈출구 (예: 링크 속성 — v0.1 제약)"""
        return self.client.conn.execute(query, params).fetchall()


class FunctionEngine:
    def __init__(self, client: OntologyClient):
        self.client = client
        self._modules: dict[str, ModuleType] = {}

    # ------------------------------------------------------------------ 구현 찾기
    def _module(self, module: str) -> ModuleType:
        if module in self._modules:
            return self._modules[module]
        if self.client.base_dir is None:
            raise FunctionError("구현 파일 위치(base_dir)를 모릅니다. OntologyClient.open(경로)로 여세요")
        file = self.client.base_dir / (module.replace(".", "/") + ".py")
        if not file.exists():
            raise FunctionError(f"구현 파일이 없습니다: {file}")
        # 온톨로지마다 같은 'functions' 모듈 이름을 쓰므로 고유한 이름으로 불러온다
        name = f"_minionto_{self.client.onto.ontology.name}_{module.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(name, file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        self._modules[module] = mod
        return mod

    def implementation(self, name: str):
        fn_def = self.client.onto.functions.get(name)
        if fn_def is None:
            raise FunctionError(f"Function '{name}' 이 정의되어 있지 않습니다 "
                                f"(있는 Function: {', '.join(self.client.onto.functions) or '없음'})")
        module, func = fn_def.implementation.split(":")
        impl = getattr(self._module(module), func, None)
        if not callable(impl):
            raise FunctionError(f"{module}.py 에 함수 '{func}' 가 없습니다")
        return fn_def, impl

    # ------------------------------------------------------------------ 호출
    def call(self, name: str, **args: Any) -> Any:
        fn_def, impl = self.implementation(name)
        unknown = set(args) - set(fn_def.inputs)
        if unknown:
            raise FunctionError(f"{name}: 정의에 없는 입력 {sorted(unknown)}")
        kwargs = {}
        for pname, param in fn_def.inputs.items():
            if args.get(pname) is None:
                if param.required and param.default is None:
                    raise FunctionError(f"{name}: 필수 입력 '{pname}' 이 없습니다")
                kwargs[pname] = param.default
                continue
            kwargs[pname] = self.to_input(param, args[pname], f"{name}.{pname}")
        try:
            result = impl(FunctionContext(self.client), **kwargs)
        except (FunctionError, QueryError):
            raise
        except Exception as e:  # noqa: BLE001 — 구현 오류에 Function 이름을 붙여 다시 던진다
            raise FunctionError(f"{name} 실행 중 오류: {type(e).__name__}: {e}") from e
        self.check_output(fn_def.returns, result, f"{name} 반환값")
        return result

    # ------------------------------------------------------------------ 입력 변환·출력 검사
    def to_input(self, param: Parameter, value: Any, where: str) -> Any:
        from .objects import OntoObject
        if param.type == "object":
            if isinstance(value, OntoObject):
                if value.object_type != param.object_type:
                    raise FunctionError(f"{where}: {param.object_type} 객체가 필요한데 {value.object_type} 입니다")
                return value
            obj = self.client.get(param.object_type, value)
            if obj is None:
                raise FunctionError(f"{where}: {param.object_type} '{value}' 가 없습니다")
            return obj
        if param.type == "list":
            item = Parameter(type=param.items, object_type=param.object_type)
            return [self.to_input(item, v, where) for v in value]
        if param.type == "struct":
            return value
        if param.type == "enum" and str(value) not in (param.values or []):
            raise FunctionError(f"{where}: '{value}' 은 허용 값이 아닙니다 ({', '.join(param.values)})")
        try:
            return coerce(_as_property(param), value, where)
        except QueryError as e:
            raise FunctionError(str(e)) from None

    def check_output(self, param: Parameter, value: Any, where: str) -> None:
        from .objects import OntoObject
        if value is None:
            if param.type in ("boolean",):
                raise FunctionError(f"{where}: boolean 을 돌려줘야 하는데 None 입니다")
            return
        expected = {
            "boolean": bool, "integer": int, "float": (int, float), "decimal": (int, float, Decimal),
            "string": str, "enum": str, "date": date, "datetime": datetime, "list": (list, tuple),
            "struct": dict, "object": OntoObject,
        }[param.type]
        if not isinstance(value, expected) or (param.type == "integer" and isinstance(value, bool)):
            raise FunctionError(f"{where}: {param.type} 이어야 하는데 {type(value).__name__} 입니다")
        if param.type == "enum" and value not in param.values:
            raise FunctionError(f"{where}: '{value}' 은 허용 값이 아닙니다")
        if param.type == "object" and value.object_type != param.object_type:
            raise FunctionError(f"{where}: {param.object_type} 이어야 하는데 {value.object_type} 입니다")
        if param.type == "list":
            item = Parameter(type=param.items, object_type=param.object_type)
            for i, v in enumerate(value):
                self.check_output(item, v, f"{where}[{i}]")
        if param.type == "struct":
            missing = set(param.fields) - set(value)
            if missing:
                raise FunctionError(f"{where}: 필드 {sorted(missing)} 가 없습니다")
            for fname, fparam in param.fields.items():
                self.check_output(fparam, value[fname], f"{where}.{fname}")


def _as_property(param: Parameter):
    from .metamodel import Property
    return Property(type=param.type, values=param.values if param.type == "enum" else None)
