"""MiniOnto SDK — Ontology API 를 로컬 엔진과 같은 모양으로 쓰는 원격 클라이언트 (19장)

    onto = RemoteOntology("http://localhost:8800", "taxos", token="...")
    rows = (onto.objects("TaxReturn")
            .where(status="자료수집중")
            .where({"field": "customer.risk_level", "op": "eq", "value": "높음"})
            .order_by("due_date").limit(5).all())
    res = onto.apply("request_missing_documents", "R00006", dry_run=True)

이 모듈은 httpx 만 쓴다(DB 드라이버 불필요). `minionto sdk` 명령이 만드는 타입 SDK 는
이 클래스를 상속해 객체 타입별 dataclass 와 Action·Function 메서드를 덧붙인다.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Generic, TypeVar

import httpx

T = TypeVar("T")


class SDKError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        self.status, self.detail = status, detail
        super().__init__(f"HTTP {status}: {detail}")


def conditions(cond: dict | None = None, **eq: Any) -> list[dict]:
    """where() 인자 → 조건식 목록. 로컬 ObjectSet.where 와 같은 규칙 (목록 → in, None → is_null)"""
    out = [cond] if cond is not None else []
    for k, v in eq.items():
        if isinstance(v, (list, tuple, set)):
            out.append({"field": k, "op": "in", "value": list(v)})
        elif v is None:
            out.append({"field": k, "op": "is_null"})
        else:
            out.append({"field": k, "op": "eq", "value": v.isoformat() if isinstance(v, date) else v})
    return out


class Record(dict):
    """응답 객체. r["name"] 과 r.name 둘 다 된다"""
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass
class ActionResponse:
    status: str
    action: str
    target: Any
    messages: list[str]
    changes: list[dict]
    events: list[str]
    approval_id: str | None
    dry_run: bool
    execution_id: str = ""
    object_type: str = ""
    transitions: list[dict] = field(default_factory=list)      # 20장 (이전 서버는 보내지 않음)
    retryable: bool = False                                     # 22장
    error_code: str | None = None
    attempts: int = 1
    replayed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @classmethod
    def from_dict(cls, d: dict) -> ActionResponse:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def __str__(self) -> str:
        head = f"[{self.status}{' · dry-run' if self.dry_run else ''}] {self.action}({self.target})"
        lines = [head] + [f"  - {m}" for m in self.messages]
        lines += [f"  * {c['op']} {c['object_type']}({c['pk']})" for c in self.changes]
        if self.events:
            lines.append(f"  → 이벤트 {', '.join(self.events)}")
        return "\n".join(lines)


# ====================================================================== 원격 객체 집합
class RemoteObjectSet:
    """서버에 보낼 질의를 쌓기만 한다. all·count 등을 부를 때 HTTP 요청을 보낸다."""

    def __init__(self, client: RemoteOntology, object_type: str, *, steps: tuple = (),
                 order: tuple[str, ...] = (), limit_: int = 20, offset_: int = 0):
        self.client, self.object_type = client, object_type      # object_type: 질의를 시작한 타입 (URL)
        self._current: str | None = object_type                    # pivot 을 거친 뒤 결과의 타입
        self._steps, self._order, self._limit, self._offset = steps, order, limit_, offset_

    def _copy(self, **kw) -> RemoteObjectSet:
        new = object.__new__(type(self))
        new.__dict__.update(self.__dict__)
        for k, v in kw.items():
            setattr(new, k, v)
        return new

    def where(self, cond: dict | None = None, /, **eq: Any) -> RemoteObjectSet:
        return self._copy(_steps=self._steps + tuple({"where": c} for c in conditions(cond, **eq)))

    def pivot(self, link: str) -> RemoteObjectSet:
        target = self.client.link_target(self._current, link) if self._current else None
        return self._copy(_steps=self._steps + ({"pivot": link},), _current=target)

    def order_by(self, *paths: str) -> RemoteObjectSet:
        return self._copy(_order=self._order + paths)

    def limit(self, n: int) -> RemoteObjectSet:
        return self._copy(_limit=n)

    def offset(self, n: int) -> RemoteObjectSet:
        return self._copy(_offset=n)

    def _body(self, **extra: Any) -> dict:
        return {"steps": list(self._steps), **extra}

    def _search(self, select: list[str] | None = None) -> dict:
        return self.client.post(f"objects/{self.object_type}/search", self._body(
            order_by=list(self._order), limit=self._limit, offset=self._offset, select=select))

    def _wrap(self, row: dict) -> Any:
        model = self.client.models.get(self._current) if self._current else None
        return model.from_dict(row) if model else Record(row)

    def all(self) -> list:
        return [self._wrap(r) for r in self._search()["data"]]

    def __iter__(self) -> Iterator:
        return iter(self.all())

    def first(self):
        rows = self.limit(1).all()
        return rows[0] if rows else None

    def count(self) -> int:
        return self.client.post(f"objects/{self.object_type}/aggregate", self._body())["count"]

    def rows(self, *paths: str) -> list[Record]:
        return [Record(r) for r in self._search(list(paths))["data"]]

    def count_by(self, *paths: str) -> list[Record]:
        res = self.client.post(f"objects/{self.object_type}/aggregate", self._body(count_by=list(paths)))
        return [Record(r) for r in res["groups"]]

    def aggregate(self, **metrics: tuple[str, str] | str) -> Record:
        spec = {k: [v] if isinstance(v, str) else list(v) for k, v in metrics.items()}
        return Record(self.client.post(f"objects/{self.object_type}/aggregate",
                                       self._body(metrics=spec))["metrics"])

    def __repr__(self) -> str:
        return f"<RemoteObjectSet {self.object_type} steps={len(self._steps)}>"


class TypedObjectSet(RemoteObjectSet, Generic[T]):
    """생성된 SDK 용 타입 표시. 결과를 객체 타입 dataclass 로 받는다 (변환은 RemoteObjectSet._wrap)"""

    def all(self) -> list[T]:
        return super().all()

    def first(self) -> T | None:
        return super().first()


# ====================================================================== 클라이언트
class RemoteOntology:
    # 생성된 SDK 가 채운다
    ontology: ClassVar[str | None] = None
    version: ClassVar[str | None] = None
    models: ClassVar[dict[str, type]] = {}
    link_targets: ClassVar[dict[str, dict[str, str]]] = {}

    def __init__(self, base_url: str, ontology: str | None = None, token: str | None = None, *,
                 http: httpx.Client | None = None, timeout: float = 30.0):
        self.name = ontology or self.ontology
        if not self.name:
            raise ValueError("온톨로지 이름이 필요합니다")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = http or httpx.Client(base_url=base_url, timeout=timeout)
        self.http.headers.update(headers)
        self.prefix = f"/v1/ontologies/{self.name}"

    # ------------------------------------------------------------------ HTTP
    def _check(self, r: httpx.Response, ok: tuple[int, ...] = (200,)) -> Any:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        if r.status_code not in ok:
            raise SDKError(r.status_code, body.get("detail", body) if isinstance(body, dict) else body)
        return body

    def get(self, path: str, **params: Any) -> Any:
        return self._check(self.http.get(f"{self.prefix}/{path}".rstrip("/"), params=params))

    def post(self, path: str, body: dict) -> Any:
        return self._check(self.http.post(f"{self.prefix}/{path}", json=body))

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 메타데이터
    def metadata(self) -> dict:
        return self.get("")

    def me(self) -> Record:
        """이 토큰의 실행자·범위·적용되는 정책 (21장)"""
        return Record(self.metadata()["you"])

    def check_version(self) -> str:
        """생성된 SDK 와 서버 버전 비교. 주 버전이 다르면(호환 깨짐) 오류"""
        server = self.metadata()["version"]
        if self.version and server.split(".")[0] != self.version.split(".")[0]:
            raise SDKError(409, f"SDK 는 v{self.version} 용인데 서버는 v{server} 입니다. SDK 를 다시 생성하세요")
        return server

    def link_target(self, object_type: str, link: str) -> str | None:
        return self.link_targets.get(object_type, {}).get(link)

    # ------------------------------------------------------------------ 객체
    def objects(self, object_type: str) -> RemoteObjectSet:
        return TypedObjectSet(self, object_type)

    def get_object(self, object_type: str, pk: Any) -> Record | None:
        r = self.http.get(f"{self.prefix}/objects/{object_type}/{pk}")
        if r.status_code == 404:
            return None
        body = self._check(r)
        model = self.models.get(object_type)
        return model.from_dict(body["properties"]) if model else Record(body["properties"])

    def linked(self, object_type: str, pk: Any, link: str, limit: int = 20) -> Any:
        body = self.get(f"objects/{object_type}/{pk}/links/{link}", limit=limit)
        model = self.models.get(body["object_type"])
        conv = (lambda o: model.from_dict(o["properties"])) if model else (lambda o: Record(o["properties"]))
        if not body["many"]:
            return conv(body["data"]) if body["data"] else None
        return [conv(o) for o in body["data"]]

    # ------------------------------------------------------------------ Function · Action
    def call(self, function: str, **args: Any) -> Any:
        return self.post(f"functions/{function}", {"args": args})["result"]

    def candidates(self, action: str, limit: int = 20) -> list[Record]:
        return [Record(r) for r in self.get(f"actions/{action}/candidates", limit=limit)["data"]]

    def apply(self, action: str, target: Any, *, params: dict | None = None,
              dry_run: bool = False, expect: dict | None = None,
              idempotency_key: str | None = None) -> ActionResponse:
        """idempotency_key 를 주면 네트워크 오류로 다시 보내도 한 번만 실행된다 (22장)"""
        body = {"target": target, "params": params or {}, "dry_run": dry_run}
        if expect:
            body["expect"] = expect
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        r = self.http.post(f"{self.prefix}/actions/{action}/apply", json=body, headers=headers)
        body = self._check(r, ok=(200, 202, 403, 409, 422, 500, 503))
        if "status" not in body:                         # Action 결과가 아닌 오류 (예: 404)
            raise SDKError(r.status_code, body)
        return ActionResponse.from_dict(body)

    def available(self, object_type: str, pk: Any) -> Record:
        """지금 이 객체에 할 수 있는 일: {state, actions: [{action, status, messages, next_state, missing}]}"""
        body = self.get(f"objects/{object_type}/{pk}/actions")
        body["actions"] = [Record(a) for a in body["actions"]]
        return Record(body)

    def history(self, object_type: str, pk: Any) -> list[Record]:
        """상태 전이 이력 (오래된 것부터)"""
        return [Record(r) for r in self.get(f"objects/{object_type}/{pk}/history")["data"]]

    def approvals(self) -> list[Record]:
        return [Record(r) for r in self.get("approvals")["data"]]

    def decide(self, approval_id: str, approve: bool = True, comment: str | None = None) -> ActionResponse:
        r = self.http.post(f"{self.prefix}/approvals/{approval_id}/decide",
                           json={"approve": approve, "comment": comment})
        return ActionResponse.from_dict(self._check(r, ok=(200, 202, 403, 409, 422, 500)))


# ====================================================================== 생성 코드용 변환
def to_date(v: Any) -> date | None:
    return date.fromisoformat(v) if isinstance(v, str) else v


def to_datetime(v: Any) -> datetime | None:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def to_decimal(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def drop_none(**kw: Any) -> dict:
    return {k: v for k, v in kw.items() if v is not None}
