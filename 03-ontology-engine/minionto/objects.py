"""Object Engine · Link Engine — 온톨로지 이름으로 객체를 조회하고 링크를 따라간다

    client = OntologyClient.open("ontologies/taxos/ontology.yaml", today="2027-01-18")
    returns = (client.objects("TaxReturn")
               .where(status="자료수집중")
               .where({"field": "customer.risk_level", "op": "eq", "value": "높음"})
               .order_by("due_date"))
    print(returns.sql())          # 생성된 SQL 확인
    for r in returns.limit(5):
        print(r, r.customer.accountant.name)           # 링크 따라가기

사용하는 쪽은 테이블·컬럼·조인 키를 몰라도 된다. 업무 용어(객체·속성·링크)만 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .loader import load
from .metamodel import Condition, LinkQuantifier, Ontology
from .query import Bindings, Compiler, QueryError, Scope, coerce
from .schema import LinkEnd, Schema

AGGREGATES = {"count", "sum", "avg", "min", "max"}


class OntologyClient:
    """온톨로지 정의 + DB 연결. 모든 조회의 출발점."""

    def __init__(self, onto: Ontology, url: str | None = None, *,
                 today: date | str | None = None, actor: dict | None = None):
        self.onto = onto
        self.schema = Schema(onto)
        self._url = url                         # 생략하면 연결할 때 .env 에서 읽는다
        self.today = date.fromisoformat(today) if isinstance(today, str) else today
        self.actor = actor or {}
        self._conn: psycopg.Connection | None = None
        self.query_log: list[str] = []          # 실행한 SQL (교육·디버깅용)

    @classmethod
    def open(cls, path: str | Path, url: str | None = None, **kw) -> OntologyClient:
        return cls(load(path), url, **kw)

    # ------------------------------------------------------------------ 연결
    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            from .db import database_url
            self._conn = psycopg.connect(database_url(self.onto, self._url), autocommit=True,
                                         row_factory=dict_row, connect_timeout=5)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> OntologyClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def fetch(self, query: sql.Composable) -> list[dict]:
        self.query_log.append(query.as_string(self.conn))
        return self.conn.execute(query).fetchall()

    # ------------------------------------------------------------------ 진입점
    def objects(self, object_type: str) -> ObjectSet:
        if object_type not in self.onto.object_types:
            raise QueryError(f"객체 타입 '{object_type}' 이 없습니다 "
                             f"(있는 타입: {', '.join(self.onto.object_types)})")
        return ObjectSet(self, object_type)

    def get(self, object_type: str, pk: Any) -> OntoObject | None:
        ot = self.onto.object_types[object_type]
        return self.objects(object_type).where(**{ot.source.primary_key: pk}).first()

    def bindings(self, params: dict | None = None, actor: dict | None = None) -> Bindings:
        return Bindings(actor=actor if actor is not None else self.actor, params=params or {},
                        today=self.today, now=datetime.now())


def to_condition(cond: Condition | dict | None = None, **eq: Any) -> list[Condition]:
    """where() 인자를 조건식 목록으로: dict·Condition 또는 속성=값 키워드(eq)"""
    out: list[Condition] = []
    if cond is not None:
        out.append(cond if isinstance(cond, Condition) else Condition.model_validate(cond))
    for k, v in eq.items():
        if isinstance(v, (list, tuple, set)):
            out.append(Condition(field=k, op="in", value=list(v)))
        elif v is None:
            out.append(Condition(field=k, op="is_null"))
        else:
            out.append(Condition(field=k, op="eq", value=v))
    return out


class ObjectSet:
    """같은 타입 객체의 집합을 '정의'한다. 실행(all·count 등) 전까지 DB를 읽지 않는다.

    where·pivot·order_by·limit 는 원본을 바꾸지 않고 새 ObjectSet 을 돌려준다.
    """

    def __init__(self, client: OntologyClient, object_type: str, conditions: tuple[Condition, ...] = (),
                 order: tuple[tuple[str, bool], ...] = (), limit_: int | None = None, offset_: int = 0,
                 params: dict | None = None):
        self.client = client
        self.object_type = object_type
        self.conditions = conditions
        self.order = order
        self._limit = limit_
        self._offset = offset_
        self.params = params or {}

    def _copy(self, **kw) -> ObjectSet:
        base = dict(conditions=self.conditions, order=self.order, limit_=self._limit,
                    offset_=self._offset, params=self.params)
        base.update(kw)
        return ObjectSet(self.client, self.object_type, **base)

    def __repr__(self) -> str:
        return f"<ObjectSet {self.object_type} 조건 {len(self.conditions)}개>"

    # ------------------------------------------------------------------ 집합 정의
    def where(self, cond: Condition | dict | None = None, /, **eq: Any) -> ObjectSet:
        """조건 추가 (AND). 예: where(status="자료수집중"), where({"some": {...}})"""
        return self._copy(conditions=self.conditions + tuple(to_condition(cond, **eq)))

    def bind(self, **params: Any) -> ObjectSet:
        """조건식 안의 $params.<이름> 값을 정한다"""
        return self._copy(params={**self.params, **params})

    def pivot(self, link: str) -> ObjectSet:
        """링크로 이어진 객체 집합으로 이동 (Search Around)

        objects("Accountant").where(accountant_id="A002").pivot("customers")
          = A002 가 담당하는 고객들
        구현: 도착 타입 기준 '역방향 링크로 되돌아가면 원래 집합에 속하는가' (EXISTS)
        """
        if self._limit is not None or self._offset:
            raise QueryError("limit/offset 이 걸린 집합은 pivot 할 수 없습니다 (조건만 이어진다)")
        schema = self.client.schema
        end = schema.links[self.object_type].get(link)
        if end is None:
            raise QueryError(f"'{self.object_type}' 에 '{link}' 링크가 없습니다 "
                             f"(있는 링크: {', '.join(schema.links[self.object_type]) or '없음'})")
        back = reverse_end(schema, end)
        where = Condition(all_=list(self.conditions)) if self.conditions else None
        cond = Condition(some=LinkQuantifier(link=back.name, where=where))
        return ObjectSet(self.client, end.target, (cond,), params=self.params)

    def order_by(self, *paths: str) -> ObjectSet:
        """정렬. '-due_date' 처럼 앞에 - 를 붙이면 내림차순. 단일 값 경로만 가능"""
        return self._copy(order=self.order + tuple((p.lstrip("-"), p.startswith("-")) for p in paths))

    def limit(self, n: int) -> ObjectSet:
        return self._copy(limit_=n)

    def offset(self, n: int) -> ObjectSet:
        return self._copy(offset_=n)

    # ------------------------------------------------------------------ SQL 생성
    def _compile(self, select: list[tuple[str, str]] | None = None, group: list[str] | None = None,
                 aggregates: list[tuple[str, str, str | None]] | None = None,
                 paged: bool = True) -> sql.Composable:
        """select: (경로, 별칭) · group: 그룹 경로 · aggregates: (별칭, 함수, 경로)"""
        comp = Compiler(self.client.schema, self.client.bindings(self.params))
        scope = comp.scope(self.object_type)
        ot = self.client.onto.object_types[self.object_type]

        cols: list[sql.Composable] = []
        if select is None and group is None:
            select = [(p, p) for p in ot.properties]
        for path, alias in select or []:
            col, _ = scope.column(path)
            cols.append(sql.SQL("{} AS {}").format(col, sql.Identifier(alias)))
        group_cols = []
        for path in group or []:
            col, _ = scope.column(path)
            group_cols.append(col)
            cols.append(sql.SQL("{} AS {}").format(col, sql.Identifier(path)))
        for alias, fn, path in aggregates or []:
            arg = sql.SQL("*") if path is None else scope.column(path)[0]
            cols.append(sql.SQL("{}({}) AS {}").format(sql.SQL(fn.upper()), arg, sql.Identifier(alias)))

        where = [comp.condition(c, scope) for c in self.conditions]
        order = []
        for path, desc in self.order:
            col, prop = scope.column(path)
            if prop.type == "enum":   # enum 은 가나다순이 아니라 정의한 값 순서로 (낮음 < 보통 < 높음)
                col = sql.SQL("array_position(ARRAY[{}]::text[], {}::text)").format(
                    sql.SQL(", ").join(sql.Literal(v) for v in prop.values), col)
            order.append(col + sql.SQL(" DESC NULLS LAST" if desc else " ASC"))
        if select is not None and group is None and not aggregates:
            pk_col, _ = scope.column(ot.source.primary_key)   # 결과 순서를 항상 고정
            order.append(pk_col + sql.SQL(" ASC"))

        parts = [sql.SQL("SELECT {}\nFROM {} {}").format(
            sql.SQL(",\n       ").join(cols), sql.Identifier(ot.source.table), sql.Identifier(scope.alias))]
        parts += scope.joins                                    # 경로 해석 중 추가된 LEFT JOIN
        if where:
            parts.append(sql.SQL("WHERE ") + sql.SQL(" AND ").join(where))
        if group_cols:
            parts.append(sql.SQL("GROUP BY ") + sql.SQL(", ").join(group_cols))
        if order:
            parts.append(sql.SQL("ORDER BY ") + sql.SQL(", ").join(order))
        if paged and self._limit is not None:
            parts.append(sql.SQL("LIMIT {}").format(sql.Literal(self._limit)))
        if paged and self._offset:
            parts.append(sql.SQL("OFFSET {}").format(sql.Literal(self._offset)))
        return sql.SQL("\n").join(parts)

    def sql(self) -> str:
        return self._compile().as_string()

    # ------------------------------------------------------------------ 실행
    def all(self) -> list[OntoObject]:
        rows = self.client.fetch(self._compile())
        return [OntoObject(self.client, self.object_type, r) for r in rows]

    def __iter__(self) -> Iterator[OntoObject]:
        return iter(self.all())

    def first(self) -> OntoObject | None:
        found = self.limit(1).all()
        return found[0] if found else None

    def one(self) -> OntoObject:
        found = self.limit(2).all()
        if len(found) != 1:
            raise QueryError(f"{self.object_type} 객체가 정확히 하나가 아닙니다 ({len(found)}개 이상)")
        return found[0]

    def count(self) -> int:
        """집합의 객체 수 (limit 는 무시)"""
        q = self._copy(order=())._compile(aggregates=[("n", "count", None)], group=[], paged=False)
        return self.client.fetch(q)[0]["n"]

    def exists(self) -> bool:
        return self.first() is not None

    def rows(self, *paths: str) -> list[dict]:
        """표 형태 조회. 링크를 거친 속성도 가능: rows("period", "customer.company_name")"""
        return self.client.fetch(self._compile(select=[(p, p) for p in paths]))

    def count_by(self, *paths: str) -> list[dict]:
        """그룹별 개수. 예: count_by("customer.accountant.name")"""
        q = self._copy(order=())._compile(group=list(paths), aggregates=[("count", "count", None)],
                                          paged=False)
        rows = self.client.fetch(q)
        return sorted(rows, key=lambda r: (-r["count"], *(str(r[p]) for p in paths)))

    def aggregate(self, **specs: tuple[str, str] | str) -> dict:
        """집계. 예: aggregate(total=("sum", "freight"), n="count")"""
        aggs = []
        for alias, spec in specs.items():
            fn, path = (spec, None) if isinstance(spec, str) else spec
            if fn not in AGGREGATES:
                raise QueryError(f"집계 함수는 {', '.join(sorted(AGGREGATES))} 중 하나입니다")
            aggs.append((alias, fn, path))
        return self.client.fetch(self._copy(order=())._compile(aggregates=aggs, group=[], paged=False))[0]


def reverse_end(schema: Schema, end: LinkEnd) -> LinkEnd:
    """같은 링크 타입을 반대 방향에서 부르는 LinkEnd (자기 참조 링크도 forward 로 구분)"""
    for cand in schema.links[end.target].values():
        if cand.link_type == end.link_type and cand.forward != end.forward:
            return cand
    raise QueryError(f"링크 '{end.link_type}' 의 역방향을 찾을 수 없습니다")


class OntoObject:
    """조회된 객체 하나. 속성은 속성명으로, 링크는 링크 이름으로 접근한다.

        ret.status                    속성
        ret.customer                  단일 값 링크 → OntoObject (없으면 None)
        ret.submissions               여러 값 링크 → ObjectSet (추가 조건을 이어 붙일 수 있음)
    """

    __slots__ = ("_client", "object_type", "properties")

    def __init__(self, client: OntologyClient, object_type: str, properties: dict[str, Any]):
        self._client = client
        self.object_type = object_type
        self.properties = properties

    @property
    def _def(self):
        return self._client.onto.object_types[self.object_type]

    @property
    def pk(self) -> Any:
        return self.properties[self._def.source.primary_key]

    @property
    def title(self) -> Any:
        return self.properties.get(self._def.title_property)

    def link(self, name: str) -> ObjectSet:
        """링크 이름으로 이어진 객체 집합 (단일·여러 값 모두 ObjectSet)"""
        pk_name = self._def.source.primary_key
        return self._client.objects(self.object_type).where(**{pk_name: self.pk}).pivot(name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.properties:
            return self.properties[name]
        end = self._client.schema.links[self.object_type].get(name)
        if end is None:
            raise AttributeError(f"'{self.object_type}' 에 '{name}' 속성이나 링크가 없습니다")
        found = self.link(name)
        return found if end.many else found.first()

    def __getitem__(self, name: str) -> Any:
        return self.properties[name]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OntoObject) and (self.object_type, self.pk) == (other.object_type, other.pk)

    def __hash__(self) -> int:
        return hash((self.object_type, self.pk))

    def __repr__(self) -> str:
        return f"{self.object_type}({self.pk} {self.title!r})"

    def to_dict(self) -> dict[str, Any]:
        return {"$type": self.object_type, "$pk": self.pk, **self.properties}


__all__ = ["OntologyClient", "ObjectSet", "OntoObject", "QueryError", "coerce", "Scope"]
