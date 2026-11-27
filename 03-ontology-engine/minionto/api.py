"""Ontology API — 엔진을 REST API 로 연다 (19장)

    app = create_app([Served.from_file("ontologies/taxos/ontology.yaml")], api_keys=load_api_keys(...))
    uvicorn.run(app)          # 또는: python -m minionto serve ontologies/taxos/ontology.yaml --keys ...

엔드포인트는 온톨로지 이름 아래에 업무 용어로만 만든다. 테이블·SQL 은 드러나지 않는다.

  GET  /v1/ontologies                                   서빙 중인 온톨로지와 버전
  GET  /v1/ontologies/{o}                               공개 메타데이터 (객체·링크·Function·Action)
  POST /v1/ontologies/{o}/objects/{type}/search         조건·링크 탐색·정렬·페이지
  POST /v1/ontologies/{o}/objects/{type}/aggregate      그룹별 개수·집계
  GET  /v1/ontologies/{o}/objects/{type}/{pk}           객체 하나
  GET  /v1/ontologies/{o}/objects/{type}/{pk}/links/{link}
  GET  /v1/ontologies/{o}/objects/{type}/{pk}/actions   지금 이 객체에 할 수 있는 일 (20장)
  GET  /v1/ontologies/{o}/objects/{type}/{pk}/history   상태 전이 이력 (20장)
  POST /v1/ontologies/{o}/functions/{fn}
  GET  /v1/ontologies/{o}/actions/{a}/candidates        내가 실행할 수 있는 대상
  POST /v1/ontologies/{o}/actions/{a}/apply             실행 (dry_run 가능)
  GET  /v1/ontologies/{o}/approvals                     내가 처리할 승인 요청
  POST /v1/ontologies/{o}/approvals/{id}/decide

실행자(actor)는 요청 본문이 아니라 인증 토큰에서 정한다. 호출하는 쪽이 자기 역할을 고를 수 없다.
21장: 서명된 토큰(JwtAuth)과 범위(scope)로 인증하고, 모든 조회·실행에 권한 정책(행·속성)을 적용한다.
19장의 개발용 API 키(ApiKeyAuth)도 그대로 쓸 수 있다.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .auth import ApiKeyAuth, AuthError, Principal
from .describe import public_metadata
from .functions import FunctionError
from .policy import PolicyError
from .metamodel import Ontology
from .objects import ObjectSet, OntologyClient, OntoObject
from .query import QueryError

# ActionResult.status → HTTP 상태 코드 (본문의 status 가 기준이고, 코드는 HTTP 도구를 위한 것)
STATUS_CODES = {"completed": 200, "approval_required": 202, "denied": 403, "rejected": 409,
                "invalid": 422, "failed": 500}
MAX_LIMIT = 500


# ====================================================================== 서빙 대상
@dataclass
class Served:
    """API 로 내보내는 온톨로지 한 개 (파일 또는 Registry 활성 버전)"""
    onto: Ontology
    version: str
    checksum: str | None = None
    function_sources: dict[str, str] = field(default_factory=dict)
    origin: str = "file"

    @property
    def name(self) -> str:
        return self.onto.ontology.name

    @classmethod
    def from_file(cls, path: str | Path) -> Served:
        from .registry import Package
        pkg = Package.from_file(path)                    # 검증을 통과해야 서빙한다
        return cls(pkg.onto, pkg.onto.ontology.version, pkg.checksum, pkg.function_sources, "file")

    @classmethod
    def from_registry(cls, v) -> Served:
        return cls(v.onto, v.version, v.checksum, v.function_sources, "registry")

    def client(self, **kw) -> OntologyClient:
        return OntologyClient(self.onto, function_sources=self.function_sources, **kw)


def load_api_keys(path: str | Path) -> dict[str, dict]:
    """개발용 키 파일: keys: {토큰: {id, role, type}}"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    keys = data.get("keys") or {}
    for token, actor in keys.items():
        if not {"id", "role", "type"} <= set(actor):
            raise ValueError(f"API 키 '{token[:4]}…' 의 실행자 정보에 id·role·type 이 모두 있어야 합니다")
    return keys


# ====================================================================== 요청 형식
class Step(BaseModel):
    where: dict | None = Field(None, description="조건식 (YAML DSL 과 같은 형식)")
    pivot: str | None = Field(None, description="이동할 링크 이름")


class SearchRequest(BaseModel):
    where: list[dict] = Field(default_factory=list, description="시작 타입에 거는 조건")
    steps: list[Step] = Field(default_factory=list, description="그다음 순서대로 적용할 조건·링크 이동")
    order_by: list[str] = Field(default_factory=list, description="정렬 경로, 내림차순은 -경로")
    select: list[str] | None = Field(None, description="돌려받을 경로 (링크 경로 가능). 생략하면 전체 속성")
    limit: int = Field(20, ge=1, le=MAX_LIMIT)
    offset: int = Field(0, ge=0)


class AggregateRequest(BaseModel):
    where: list[dict] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    count_by: list[str] | None = Field(None, description="그룹 경로 — 그룹별 개수")
    metrics: dict[str, list[str]] | None = Field(
        None, description='{"이름": ["sum", "경로"]} 또는 {"n": ["count"]}')


class FunctionRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class ApplyRequest(BaseModel):
    target: Any
    params: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    expect: dict[str, Any] | None = Field(
        None, description='읽어 둔 값 (예: {"status": "검토중"}). 그사이 바뀌었으면 409 rejected')


class DecideRequest(BaseModel):
    approve: bool = True
    comment: str | None = None


# ====================================================================== 직렬화
def plain(value: Any) -> Any:
    """응답용 값: 객체는 {object_type, pk, label, properties}, Decimal 은 정밀도를 지키려 문자열"""
    if isinstance(value, OntoObject):
        out = {"object_type": value.object_type, "pk": value.pk, "label": value.label,
               "properties": plain(value.properties)}
        if value.restricted:
            out["hidden"] = sorted(value.restricted)
        return out
    if isinstance(value, ObjectSet):
        return [plain(o) for o in value.all()]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return jsonable_encoder(value)


# ====================================================================== 앱
def create_app(served: list[Served] | dict[str, Served], *, api_keys: dict[str, dict] | None = None,
               auth: Any = None, today: date | str | None = None,
               connect: Callable[[Served], Any] | None = None) -> FastAPI:
    """auth: authenticate(token) → Principal 을 가진 객체 (JwtAuth). 없으면 api_keys 로 ApiKeyAuth.
    connect(served) 는 OntologyClient 를 내주는 컨텍스트 매니저. 기본은 요청마다 새 연결.
    테스트는 트랜잭션을 열어 둔 클라이언트를 내주는 connect 를 넘겨 모든 변경을 롤백한다."""
    catalog = served if isinstance(served, dict) else {s.name: s for s in served}
    if auth is None:
        if api_keys is None:
            raise ValueError("auth 또는 api_keys 가 필요합니다")
        auth = ApiKeyAuth(api_keys)

    @contextmanager
    def default_connect(s: Served) -> Iterator[OntologyClient]:
        client = s.client(today=today)
        try:
            yield client
        finally:
            client.close()

    connect = connect or default_connect
    app = FastAPI(title="MiniOnto Ontology API", version="0.1.0",
                  description="온톨로지 객체 조회·Function·Action 실행 API (교육용)")
    bearer = HTTPBearer(auto_error=False)

    # ------------------------------------------------------------------ 의존성
    def current_principal(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if cred is None:
            raise HTTPException(401, "인증이 필요합니다 (Authorization: Bearer <토큰>)")
        try:
            return auth.authenticate(cred.credentials)
        except AuthError as e:
            raise HTTPException(401, str(e)) from None

    def current_actor(principal: Principal = Depends(current_principal)) -> dict:
        return dict(principal.actor)

    def served_of(ontology: str) -> Served:
        if ontology not in catalog:
            raise HTTPException(404, f"온톨로지 '{ontology}' 를 서빙하고 있지 않습니다")
        return catalog[ontology]

    def client_of(s: Served = Depends(served_of)) -> Iterator[OntologyClient]:
        with connect(s) as client:
            yield client

    def require(scope: str):
        """이 엔드포인트에 필요한 토큰 범위. 없으면 403 + 정책 판단 기록"""
        def dep(principal: Principal = Depends(current_principal),
                client: OntologyClient = Depends(client_of)) -> dict:
            if not principal.has(scope):
                client.policy.log(principal.actor, "deny", "scope", detail={"scope": scope,
                                                                             "scopes": sorted(principal.scopes)})
                raise HTTPException(403, f"토큰 범위(scope)에 '{scope}' 가 없습니다 "
                                         f"(이 토큰: {', '.join(sorted(principal.scopes)) or '없음'})")
            return dict(principal.actor)
        return dep

    def check_type(client: OntologyClient, object_type: str) -> None:
        if object_type not in client.onto.object_types:
            raise HTTPException(404, f"객체 타입 '{object_type}' 이 없습니다")

    def build(client: OntologyClient, object_type: str, where: list[dict], steps: list[Step],
              actor: dict) -> ObjectSet:
        check_type(client, object_type)
        q = client.objects(object_type).secure(actor)          # 21장: 실행자에게 보이는 범위
        for cond in where:
            q = q.where(cond)
        for step in steps:
            if (step.where is None) == (step.pivot is None):
                raise QueryError("step 에는 where 와 pivot 중 하나만 씁니다")
            q = q.where(step.where) if step.where is not None else q.pivot(step.pivot)
        return q

    @app.exception_handler(QueryError)
    def _query_error(request: Request, exc: QueryError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PolicyError)
    def _policy_error(request: Request, exc: PolicyError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(FunctionError)
    def _function_error(request: Request, exc: FunctionError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ------------------------------------------------------------------ 메타데이터
    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "ontologies": sorted(catalog)}

    @app.get("/v1/ontologies", tags=["meta"])
    def list_ontologies(actor: dict = Depends(current_actor)):
        return [{"name": s.name, "version": s.version, "checksum": s.checksum, "origin": s.origin,
                 "display_name": s.onto.ontology.display_name} for s in catalog.values()]

    @app.get("/v1/ontologies/{ontology}", tags=["meta"])
    def metadata(s: Served = Depends(served_of), principal: Principal = Depends(current_principal)):
        meta = public_metadata(s.onto, version=s.version, checksum=s.checksum)
        actor = {k: v for k, v in principal.actor.items() if k != "token_id"}
        meta["you"] = {"actor": actor, "scopes": sorted(principal.scopes), "auth": principal.method,
                       "expires_at": principal.expires_at.isoformat() if principal.expires_at else None,
                       **s.client().policy.describe_for(principal.actor)}
        return meta

    # ------------------------------------------------------------------ Object · Link
    @app.post("/v1/ontologies/{ontology}/objects/{object_type}/search", tags=["objects"])
    def search(object_type: str, body: SearchRequest, client: OntologyClient = Depends(client_of),
               actor: dict = Depends(require("objects:read"))):
        q = build(client, object_type, body.where, body.steps, actor)
        page = q.order_by(*body.order_by).offset(body.offset).limit(body.limit)
        data = page.rows(*body.select) if body.select else [o.properties for o in page.all()]
        return {"object_type": q.object_type, "total": q.count(), "limit": body.limit,
                "offset": body.offset, "hidden": sorted(client.policy.hidden(q.object_type, actor)),
                "data": plain(data)}

    @app.post("/v1/ontologies/{ontology}/objects/{object_type}/aggregate", tags=["objects"])
    def aggregate(object_type: str, body: AggregateRequest, client: OntologyClient = Depends(client_of),
                  actor: dict = Depends(require("objects:read"))):
        q = build(client, object_type, body.where, body.steps, actor)
        out: dict[str, Any] = {"object_type": q.object_type}
        if body.count_by:
            out["groups"] = plain(q.count_by(*body.count_by))
        if body.metrics:
            out["metrics"] = plain(q.aggregate(**{k: (v[0] if len(v) == 1 else (v[0], v[1]))
                                                  for k, v in body.metrics.items()}))
        if len(out) == 1:
            out["count"] = q.count()
        return out

    def visible_object(client: OntologyClient, object_type: str, pk: str, actor: dict):
        """실행자에게 보이는 객체. 없거나 보이지 않으면 똑같이 404 (존재 여부도 알려 주지 않는다)"""
        value = pk_value(client, object_type, pk)
        obj = client.get(object_type, value, actor=actor)
        if obj is None:
            if client.get(object_type, value) is not None:          # 있지만 범위 밖 → 판단 기록
                names = ", ".join(n for n, _ in client.policy.row_rules(object_type, actor))
                client.policy.log(actor, "deny", "row", object_type, str(value), policy=names,
                                  detail={"use": "조회"})
            raise HTTPException(404, f"{object_type}({pk}) 를 찾을 수 없습니다")
        return obj

    def pk_value(client: OntologyClient, object_type: str, pk: str) -> Any:
        ot = client.onto.object_types[object_type]
        if ot.properties[ot.source.primary_key].type == "integer":
            try:
                return int(pk)
            except ValueError:
                raise HTTPException(404, f"{object_type}({pk}) 를 찾을 수 없습니다") from None
        return pk

    @app.get("/v1/ontologies/{ontology}/objects/{object_type}/{pk}", tags=["objects"])
    def get_object(object_type: str, pk: str, client: OntologyClient = Depends(client_of),
                   actor: dict = Depends(require("objects:read"))):
        check_type(client, object_type)
        obj = visible_object(client, object_type, pk, actor)
        return {**plain(obj), "links": sorted(client.schema.links[object_type])}

    @app.get("/v1/ontologies/{ontology}/objects/{object_type}/{pk}/links/{link}", tags=["objects"])
    def get_linked(object_type: str, pk: str, link: str, limit: int = 20, offset: int = 0,
                   client: OntologyClient = Depends(client_of), actor: dict = Depends(require("objects:read"))):
        check_type(client, object_type)
        end = client.schema.links[object_type].get(link)
        if end is None:
            raise HTTPException(404, f"{object_type} 에 링크 '{link}' 가 없습니다")
        obj = visible_object(client, object_type, pk, actor)
        linked = obj.link(link)                                  # 링크를 따라가도 같은 실행자의 범위
        if not end.many:
            first = linked.first()
            return {"object_type": end.target, "many": False, "data": plain(first) if first else None}
        page = linked.offset(offset).limit(min(max(limit, 1), MAX_LIMIT)).all()
        return {"object_type": end.target, "many": True, "total": linked.count(), "data": plain(page)}

    @app.get("/v1/ontologies/{ontology}/objects/{object_type}/{pk}/actions", tags=["objects"])
    def available(object_type: str, pk: str, client: OntologyClient = Depends(client_of),
                  actor: dict = Depends(require("objects:read"))):
        check_type(client, object_type)
        visible_object(client, object_type, pk, actor)
        return client.actions.available(object_type, pk_value(client, object_type, pk), actor=actor).to_dict()

    @app.get("/v1/ontologies/{ontology}/objects/{object_type}/{pk}/history", tags=["objects"])
    def state_history(object_type: str, pk: str, client: OntologyClient = Depends(client_of),
                      actor: dict = Depends(require("objects:read"))):
        check_type(client, object_type)
        visible_object(client, object_type, pk, actor)
        return {"object_type": object_type, "pk": pk, "data": plain(client.actions.history(object_type, pk))}

    # ------------------------------------------------------------------ Function
    @app.post("/v1/ontologies/{ontology}/functions/{function}", tags=["functions"])
    def call_function(function: str, body: FunctionRequest, client: OntologyClient = Depends(client_of),
                      actor: dict = Depends(require("functions:call"))):
        if function not in client.onto.functions:
            raise HTTPException(404, f"Function '{function}' 이 없습니다")
        return {"function": function, "result": plain(client.functions.call_as(actor, function, **body.args))}

    # ------------------------------------------------------------------ Action
    def check_action(client: OntologyClient, action: str) -> None:
        if action not in client.onto.actions:
            raise HTTPException(404, f"Action '{action}' 이 없습니다")

    def action_response(result) -> JSONResponse:
        return JSONResponse(result.to_dict(), status_code=STATUS_CODES.get(result.status, 200))

    @app.get("/v1/ontologies/{ontology}/actions/{action}/candidates", tags=["actions"])
    def candidates(action: str, limit: int = 20, client: OntologyClient = Depends(client_of),
                   actor: dict = Depends(require("actions:apply"))):
        check_action(client, action)
        q = client.actions.candidates(action, actor=actor)
        page = q.limit(min(max(limit, 1), MAX_LIMIT)).all()
        return {"action": action, "object_type": q.object_type, "total": q.count(),
                "data": [{"pk": o.pk, "label": o.label} for o in page],
                "note": "Function 조건은 실행 시점에 검사합니다"}

    @app.post("/v1/ontologies/{ontology}/actions/{action}/apply", tags=["actions"])
    def apply(action: str, body: ApplyRequest, client: OntologyClient = Depends(client_of),
              actor: dict = Depends(require("actions:apply"))):
        check_action(client, action)
        return action_response(client.actions.apply(action, body.target, actor=actor, params=body.params,
                                                    dry_run=body.dry_run, expect=body.expect))

    @app.get("/v1/ontologies/{ontology}/approvals", tags=["actions"])
    def approvals(client: OntologyClient = Depends(client_of), actor: dict = Depends(require("approvals:decide"))):
        mine = [r for r in client.actions.pending_approvals() if actor["role"] in r["approver_roles"]]
        return {"data": plain(mine)}

    @app.post("/v1/ontologies/{ontology}/approvals/{approval_id}/decide", tags=["actions"])
    def decide(approval_id: str, body: DecideRequest, client: OntologyClient = Depends(client_of),
               actor: dict = Depends(require("approvals:decide"))):
        try:
            uuid.UUID(approval_id)
        except ValueError:
            raise HTTPException(404, f"승인 요청 '{approval_id}' 을 찾을 수 없습니다") from None
        try:
            result = client.actions.decide(approval_id, actor, approve=body.approve, comment=body.comment)
        except QueryError as e:
            raise HTTPException(409, str(e)) from None
        return action_response(result)

    return app
