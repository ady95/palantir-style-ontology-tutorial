"""MiniOnto 메타모델 — 온톨로지 YAML DSL의 구조 정의

이 모듈은 '온톨로지를 기술하는 언어'의 문법(구조)만 정의한다.
참조 무결성(링크 대상이 존재하는지 등)은 validator.py 에서 검사한다.

※ 이 DSL은 이 책의 자체 설계이며 팔란티어 공식 문법이 아니다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PropertyType = Literal["string", "integer", "decimal", "float", "boolean", "date", "datetime", "enum", "binary"]
ParameterType = Literal["string", "integer", "decimal", "float", "boolean", "date", "datetime", "enum",
                        "object", "list", "struct"]
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
Operator = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "not_null"]


class Strict(BaseModel):
    """오타를 잡기 위해 정의되지 않은 키를 허용하지 않는다."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Data: Object Type · Property · Interface · Link Type
# ---------------------------------------------------------------------------

class Property(Strict):
    type: PropertyType
    column: str | None = Field(None, description="DB 컬럼명. 생략하면 속성명과 같다.")
    display_name: str | None = None
    description: str | None = None
    required: bool = False
    values: list[str] | None = Field(None, description="enum 타입의 허용 값")

    @model_validator(mode="after")
    def _enum_values(self):
        if self.type == "enum" and not self.values:
            raise ValueError("enum 타입에는 values 가 필요합니다")
        if self.type != "enum" and self.values:
            raise ValueError("values 는 enum 타입에서만 쓸 수 있습니다")
        return self


class Source(Strict):
    table: str
    primary_key: str = Field(description="기본키 역할을 하는 '속성' 이름")
    id_strategy: Literal["provided", "uuid", "prefixed_sequence"] = "provided"
    id_prefix: str | None = Field(None, description="prefixed_sequence 일 때 접두어 (예: T)")

    @model_validator(mode="after")
    def _prefix(self):
        if self.id_strategy == "prefixed_sequence" and not self.id_prefix:
            raise ValueError("prefixed_sequence 에는 id_prefix 가 필요합니다")
        return self


class ObjectType(Strict):
    display_name: str
    description: str | None = None
    source: Source
    title_property: str = Field(description="객체를 사람이 알아보는 대표 속성")
    properties: dict[str, Property]
    implements: list[str] = Field(default_factory=list)


class InterfaceProperty(Strict):
    type: PropertyType
    description: str | None = None


class Interface(Strict):
    display_name: str
    description: str | None = None
    properties: dict[str, InterfaceProperty]


class Through(Strict):
    table: str
    from_column: str
    to_column: str


class LinkType(Strict):
    display_name: str | None = None
    description: str | None = None
    from_: str = Field(alias="from")
    to: str
    cardinality: Cardinality
    from_key: str | None = Field(None, description="from 객체의 속성. many_to_many 에서는 생략 시 기본키")
    to_key: str | None = Field(None, description="to 객체의 속성. many_to_many 에서는 생략 시 기본키")
    through: Through | None = None
    forward_name: str = Field(description="from 객체에서 to 객체로 가는 이름")
    reverse_name: str = Field(description="to 객체에서 from 객체로 가는 이름")

    @model_validator(mode="after")
    def _keys(self):
        if self.cardinality == "many_to_many":
            if not self.through:
                raise ValueError("many_to_many 링크에는 through 가 필요합니다")
        else:
            if self.through:
                raise ValueError("through 는 many_to_many 에서만 쓸 수 있습니다")
            if not (self.from_key and self.to_key):
                raise ValueError("from_key, to_key 가 필요합니다")
        return self


# ---------------------------------------------------------------------------
# Logic: 조건식 · Function
# ---------------------------------------------------------------------------

class LinkQuantifier(Strict):
    link: str
    where: Condition | None = None


class Condition(Strict):
    """조건식. 아래 형식 중 정확히 하나를 쓴다.

    - 비교:       {field: customer.risk_level, op: eq, value: 높음}
    - 논리:       {all: [...]}, {any: [...]}, {not: {...}}
    - 링크 수량:  {some: {link: tasks, where: {...}}}, {none: {...}}
    - 함수:       {function: has_capacity, args: {...}}   (참이면 통과)

    field 는 대상 객체 기준 경로(속성 또는 링크.속성)이거나 $actor.* / $params.* 참조다.
    value 가 $ 로 시작하면 참조($params.x, $actor.id, $target.x, $now, $today)로 해석한다.
    """
    field: str | None = None
    op: Operator | None = None
    value: Any = None
    all_: list[Condition] | None = Field(None, alias="all")
    any_: list[Condition] | None = Field(None, alias="any")
    not_: Condition | None = Field(None, alias="not")
    some: LinkQuantifier | None = None
    none: LinkQuantifier | None = None
    function: str | None = None
    args: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _one_form(self):
        forms = [k for k in ("field", "all_", "any_", "not_", "some", "none", "function")
                 if getattr(self, k) is not None]
        if len(forms) != 1:
            raise ValueError("조건식은 field·all·any·not·some·none·function 중 정확히 하나만 써야 합니다")
        if self.field is not None:
            if self.op is None:
                raise ValueError("field 비교에는 op 가 필요합니다")
            if self.op not in ("is_null", "not_null") and self.value is None:
                raise ValueError(f"op '{self.op}' 에는 value 가 필요합니다")
            if self.op in ("in", "not_in") and not (isinstance(self.value, list) or str(self.value).startswith("$")):
                raise ValueError(f"op '{self.op}' 의 value 는 목록이어야 합니다")
        elif self.op is not None or self.value is not None:
            raise ValueError("op·value 는 field 와 함께만 쓸 수 있습니다")
        if self.args is not None and self.function is None:
            raise ValueError("args 는 function 과 함께만 쓸 수 있습니다")
        return self


class Parameter(Strict):
    type: ParameterType
    description: str | None = None
    required: bool = True
    default: Any = None
    values: list[str] | None = None                 # enum
    object_type: str | None = None                  # object, list(of object)
    items: ParameterType | None = None              # list 의 원소 타입
    fields: dict[str, Parameter] | None = None      # struct

    @model_validator(mode="after")
    def _shape(self):
        if self.type == "enum" and not self.values:
            raise ValueError("enum 파라미터에는 values 가 필요합니다")
        if self.type == "object" and not self.object_type:
            raise ValueError("object 파라미터에는 object_type 이 필요합니다")
        if self.type == "list" and not self.items:
            raise ValueError("list 파라미터에는 items 가 필요합니다")
        if self.items == "object" and not self.object_type:
            raise ValueError("object 목록에는 object_type 이 필요합니다")
        if self.type == "struct" and not self.fields:
            raise ValueError("struct 에는 fields 가 필요합니다")
        return self


class Function(Strict):
    display_name: str | None = None
    description: str
    inputs: dict[str, Parameter] = Field(default_factory=dict)
    returns: Parameter
    implementation: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$",
                                description="'모듈경로:함수명' (예: functions:filing_delay_risk)")


# ---------------------------------------------------------------------------
# Action: 실행 조건 · 권한 · 승인 · 변경 내용
# ---------------------------------------------------------------------------

class Rule(Strict):
    message: str = Field(description="조건을 만족하지 못할 때 사용자·에이전트에게 돌려줄 설명")
    check: Condition


class Permission(Strict):
    roles: list[str] = Field(description="실행할 수 있는 역할")
    actor_types: list[Literal["human", "agent", "system"]] = Field(default_factory=lambda: ["human", "agent"])
    check: Condition | None = Field(None, description="역할 외 추가 조건 (예: 본인 담당 고객만)")


class Approval(Strict):
    required_if: Condition
    approver_roles: list[str]
    reason: str | None = None


class Edit(Strict):
    """Action이 만드는 변경. 아래 형식 중 하나를 쓴다.

    - {update: target, set: {...}}                      대상 객체 수정
    - {update_linked: <링크>, where: {...}, set: {...}} 링크로 연결된 객체들 수정
    - {create: <ObjectType>, values: {...}}             새 객체 생성
    """
    update: Literal["target"] | None = None
    update_linked: str | None = None
    create: str | None = None
    where: Condition | None = None
    set: dict[str, Any] | None = None
    values: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _one_kind(self):
        kinds = [k for k in ("update", "update_linked", "create") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError("edit 은 update·update_linked·create 중 정확히 하나여야 합니다")
        if self.create is not None:
            if not self.values or self.set or self.where:
                raise ValueError("create 에는 values 만 씁니다")
        else:
            if not self.set or self.values:
                raise ValueError("update 계열에는 set 이 필요합니다")
            if self.update is not None and self.where:
                raise ValueError("where 는 update_linked 에서만 씁니다")
        return self


class Action(Strict):
    display_name: str
    description: str | None = None
    target: str = Field(description="Action을 적용할 Object Type")
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    preconditions: list[Rule] = Field(default_factory=list)
    permission: Permission
    approval: Approval | None = None
    edits: list[Edit] = Field(min_length=1)
    emits: list[str] = Field(default_factory=list, description="실행 후 발행할 이벤트 이름 (Outbox)")


# ---------------------------------------------------------------------------
# 최상위
# ---------------------------------------------------------------------------

class OntologyInfo(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    display_name: str | None = None
    description: str | None = None


class Datasource(Strict):
    url_env: str = Field(description="DB 접속 URL을 담은 환경변수 이름")


class Ontology(Strict):
    ontology: OntologyInfo
    datasource: Datasource
    roles: list[str] = Field(default_factory=list, description="이 온톨로지에서 쓰는 사용자 역할")
    interfaces: dict[str, Interface] = Field(default_factory=dict)
    object_types: dict[str, ObjectType]
    link_types: dict[str, LinkType] = Field(default_factory=dict)
    functions: dict[str, Function] = Field(default_factory=dict)
    actions: dict[str, Action] = Field(default_factory=dict)


LinkQuantifier.model_rebuild()
Condition.model_rebuild()
Parameter.model_rebuild()
