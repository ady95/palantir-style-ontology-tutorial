"""조건식 → SQL 컴파일러 (Object Engine · Link Engine 공통)

온톨로지 정의의 '이름'(객체 타입·속성·링크)을 운영 DB의 테이블·컬럼·조인으로 번역한다.

- 단일 값 링크(하나로 이어지는 경로)  → LEFT JOIN
      TaxReturn 기준 customer.accountant.max_workload
- 여러 값 링크(some / none)            → 상관 EXISTS 서브쿼리
      { some: { link: submissions, where: {...} } }
- 다대다 링크                           → 연결 테이블(through)을 거치는 EXISTS

값은 sql.Literal 로 넣는다. 생성된 SQL을 그대로 출력해 확인할 수 있고(교육용),
값이 문자열로 SQL에 섞여 들어가는 일(SQL 인젝션)은 생기지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import sql

from .metamodel import Condition, Property
from .schema import LinkEnd, Schema


class QueryError(ValueError):
    """정의와 맞지 않는 질의 (없는 속성, 허용되지 않은 값, 다중 경로 비교 등)"""


@dataclass
class Bindings:
    """조건식 안의 $참조 값 ($actor.*, $params.*, $today, $now)"""
    actor: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    today: date | None = None
    now: datetime | None = None


_OPS = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


# ---------------------------------------------------------------------------
# 값 변환
# ---------------------------------------------------------------------------

def coerce(prop: Property | None, value: Any, where: str = "") -> Any:
    """속성 타입에 맞게 Python 값으로 바꾼다. enum 은 허용 값인지 확인한다."""
    if prop is None or value is None:
        return value
    if hasattr(value, "pk") and hasattr(value, "object_type"):   # OntoObject → 기본키 값
        value = value.pk
    try:
        match prop.type:
            case "integer":
                return int(value)
            case "decimal":
                return Decimal(str(value))
            case "float":
                return float(value)
            case "boolean":
                if isinstance(value, str):
                    if value.lower() not in ("true", "false"):
                        raise ValueError(value)
                    return value.lower() == "true"
                return bool(value)
            case "date":
                return value if isinstance(value, date) else date.fromisoformat(str(value))
            case "datetime":
                return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            case "enum":
                if str(value) not in prop.values:
                    raise QueryError(f"{where}: '{value}' 은 허용 값이 아닙니다 (허용: {', '.join(prop.values)})")
                return str(value)
            case _:
                return str(value)
    except (ValueError, InvalidOperation) as e:
        if isinstance(e, QueryError):
            raise
        raise QueryError(f"{where}: '{value}' 을 {prop.type} 로 바꿀 수 없습니다") from None


# ---------------------------------------------------------------------------
# Scope: SELECT 한 단계의 FROM 절 (루트 테이블 + 단일 값 링크 조인)
# ---------------------------------------------------------------------------

class Scope:
    def __init__(self, comp: Compiler, object_type: str, alias: str, root: Scope | None = None):
        self.comp = comp
        self.object_type = object_type
        self.alias = alias
        self.root = root or self                 # $target.* 은 가장 바깥 객체 기준
        self.joins: list[sql.Composable] = []
        self._aliases: dict[str, tuple[str, str]] = {"": (alias, object_type)}

    # 경로 끝의 속성 → (컬럼 식, 속성 정의)
    def column(self, path: str) -> tuple[sql.Composable, Property]:
        *links, last = path.split(".")
        alias, otype = self.walk(links, path)
        ot = self.comp.schema.object_type(otype)
        prop = ot.properties.get(last)
        if prop is None:
            hint = " (링크입니다. 속성까지 이어서 쓰세요)" if last in self.comp.schema.links[otype] else ""
            raise QueryError(f"'{otype}' 에 '{last}' 속성이 없습니다{hint} (경로: {path})")
        return sql.Identifier(alias, prop.column or last), prop

    # 단일 값 링크를 따라가며 필요한 LEFT JOIN 을 추가한다 (같은 경로는 한 번만)
    def walk(self, links: list[str], path: str) -> tuple[str, str]:
        alias, otype = self._aliases[""]
        for i, name in enumerate(links):
            key = ".".join(links[: i + 1])
            if key in self._aliases:
                alias, otype = self._aliases[key]
                continue
            end = self.comp.end(otype, name, path)
            if end.many:
                raise QueryError(f"'{otype}.{name}' 은 여러 객체로 이어지는 링크입니다. "
                                 f"비교·정렬에 쓸 수 없으니 some/none 또는 pivot 을 쓰세요 (경로: {path})")
            new = self.comp.new_alias()
            table = self.comp.schema.object_type(end.target).source.table
            on = self.comp.link_on(end, alias, otype, new)
            self.joins.append(sql.SQL("LEFT JOIN {} {} ON {}").format(
                sql.Identifier(table), sql.Identifier(new), on))
            self._aliases[key] = (new, end.target)
            alias, otype = new, end.target
        return alias, otype


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class Compiler:
    def __init__(self, schema: Schema, bindings: Bindings | None = None):
        self.schema = schema
        self.b = bindings or Bindings()
        self._n = -1

    def new_alias(self) -> str:
        self._n += 1
        return f"t{self._n}"

    def scope(self, object_type: str) -> Scope:
        if object_type not in self.schema.onto.object_types:
            raise QueryError(f"객체 타입 '{object_type}' 이 없습니다")
        return Scope(self, object_type, self.new_alias())

    def end(self, otype: str, name: str, path: str = "") -> LinkEnd:
        end = self.schema.links[otype].get(name)
        if end is None:
            raise QueryError(f"'{otype}' 에 '{name}' 링크가 없습니다" + (f" (경로: {path})" if path else ""))
        return end

    def _key(self, alias: str, otype: str, prop_name: str) -> sql.Identifier:
        prop = self.schema.object_type(otype).properties[prop_name]
        return sql.Identifier(alias, prop.column or prop_name)

    def link_on(self, end: LinkEnd, cur_alias: str, cur_type: str, tgt_alias: str) -> sql.Composable:
        """일대다·다대일·일대일 링크의 조인 조건: 도착.키 = 출발.키"""
        lt = self.schema.link_type(end.link_type)
        cur_key, tgt_key = (lt.from_key, lt.to_key) if end.forward else (lt.to_key, lt.from_key)
        return sql.SQL("{} = {}").format(self._key(tgt_alias, end.target, tgt_key),
                                         self._key(cur_alias, cur_type, cur_key))

    # ------------------------------------------------------------------ 조건식
    def condition(self, c: Condition, scope: Scope) -> sql.Composable:
        if c.field is not None:
            return self._compare(c, scope)
        if c.all_ is not None:
            return self._join("AND", [self.condition(x, scope) for x in c.all_], "TRUE")
        if c.any_ is not None:
            return self._join("OR", [self.condition(x, scope) for x in c.any_], "FALSE")
        if c.not_ is not None:
            # NULL 이 섞여도 '아니다'가 참이 되도록 두 값 논리로 맞춘다
            return sql.SQL("NOT COALESCE({}, FALSE)").format(self.condition(c.not_, scope))
        if c.some is not None:
            return self.exists(scope, c.some.link, c.some.where)
        if c.none is not None:
            return sql.SQL("NOT {}").format(self.exists(scope, c.none.link, c.none.where))
        raise QueryError(f"Function 조건('{c.function}')은 SQL로 바꿀 수 없습니다. "
                         "Function Engine(18장)이 객체를 읽어 Python에서 평가합니다.")

    @staticmethod
    def _join(op: str, parts: list[sql.Composable], empty: str) -> sql.Composable:
        if not parts:
            return sql.SQL(empty)
        if len(parts) == 1:
            return parts[0]
        return sql.SQL("(") + sql.SQL(f" {op} ").join(parts) + sql.SQL(")")

    def exists(self, scope: Scope, link_path: str, where: Condition | None) -> sql.Composable:
        """링크로 이어진 객체 중 where 를 만족하는 것이 하나라도 있는가"""
        *head, last = link_path.split(".")
        cur_alias, cur_type = scope.walk(head, link_path)
        end = self.end(cur_type, last, link_path)
        lt = self.schema.link_type(end.link_type)
        target = self.schema.object_type(end.target)

        sub = Scope(self, end.target, self.new_alias(), root=scope.root)
        if lt.through is None:
            from_ = sql.SQL("{} {}").format(sql.Identifier(target.source.table), sql.Identifier(sub.alias))
            link_cond = self.link_on(end, cur_alias, cur_type, sub.alias)
        else:
            # 다대다: 연결 테이블 → 도착 테이블
            th, ta = lt.through, self.new_alias()
            cur_col, tgt_col = (th.from_column, th.to_column) if end.forward else (th.to_column, th.from_column)
            cur_key = (lt.from_key if end.forward else lt.to_key) or \
                self.schema.object_type(cur_type).source.primary_key
            tgt_key = (lt.to_key if end.forward else lt.from_key) or target.source.primary_key
            from_ = sql.SQL("{} {} JOIN {} {} ON {} = {}").format(
                sql.Identifier(th.table), sql.Identifier(ta),
                sql.Identifier(target.source.table), sql.Identifier(sub.alias),
                self._key(sub.alias, end.target, tgt_key), sql.Identifier(ta, tgt_col))
            link_cond = sql.SQL("{} = {}").format(sql.Identifier(ta, cur_col),
                                                  self._key(cur_alias, cur_type, cur_key))
        conds = [link_cond]
        if where is not None:
            conds.append(self.condition(where, sub))       # sub.joins 가 여기서 채워진다
        return sql.SQL("EXISTS (SELECT 1 FROM {}{} WHERE {})").format(
            from_, sql.SQL("").join(sql.SQL(" ") + j for j in sub.joins),
            sql.SQL(" AND ").join(conds))

    # ------------------------------------------------------------------ 비교
    def _compare(self, c: Condition, scope: Scope) -> sql.Composable:
        left, prop = self.operand(c.field, scope)
        where = c.field
        if c.op == "is_null":
            return sql.SQL("{} IS NULL").format(left)
        if c.op == "not_null":
            return sql.SQL("{} IS NOT NULL").format(left)
        if c.op in ("in", "not_in"):
            values = self.value(c.value, scope, prop, where, many=True)
            if not values:
                return sql.SQL("FALSE" if c.op == "in" else "TRUE")
            expr = sql.SQL("{} IN ({})").format(left, sql.SQL(", ").join(values))
            return expr if c.op == "in" else sql.SQL("NOT COALESCE({}, FALSE)").format(expr)
        right = self.value(c.value, scope, prop, where)
        if c.op == "ne":
            return sql.SQL("{} IS DISTINCT FROM {}").format(left, right)   # NULL 도 '다르다'
        return sql.SQL("{} {} {}").format(left, sql.SQL(_OPS[c.op]), right)

    def operand(self, path: str, scope: Scope) -> tuple[sql.Composable, Property | None]:
        if path.startswith("$"):
            if path.startswith("$target."):
                return scope.root.column(path.removeprefix("$target."))
            return sql.Literal(self.ref(path)), None
        return scope.column(path)

    def value(self, raw: Any, scope: Scope, prop: Property | None, where: str, many: bool = False):
        if isinstance(raw, str) and raw.startswith("$target."):
            col, _ = scope.root.column(raw.removeprefix("$target."))
            return [col] if many else col
        if isinstance(raw, str) and raw.startswith("$"):
            raw = self.ref(raw)
        if many:
            if not isinstance(raw, (list, tuple, set)):
                raise QueryError(f"{where}: in/not_in 의 값은 목록이어야 합니다")
            return [sql.Literal(coerce(prop, v, where)) for v in raw]
        return sql.Literal(coerce(prop, raw, where))

    def ref(self, ref: str) -> Any:
        if ref == "$today":
            return self.b.today or date.today()
        if ref == "$now":
            return self.b.now or datetime.now()
        head, _, name = ref.partition(".")
        source = {"$actor": self.b.actor, "$params": self.b.params}.get(head)
        if source is None or name not in source:
            raise QueryError(f"참조 '{ref}' 의 값이 주어지지 않았습니다")
        v = source[name]
        return v.pk if hasattr(v, "pk") and hasattr(v, "object_type") else v
