"""Action Engine — 선언된 Action 을 검증하고, 하나의 트랜잭션으로 운영 DB에 반영한다 (Write-back)

    result = client.actions.apply(
        "request_missing_documents", target="R00024",
        actor={"id": "agent-01", "role": "AI비서", "type": "agent"},
        params={"note": "기한 전 제출 부탁드립니다"})
    result.status   # completed | approval_required | rejected | denied | invalid | failed

실행 순서 (모두 같은 트랜잭션)
  0. 입력 검사       파라미터 타입·필수·enum, 대상 객체 존재
  1. 대상 잠금       SELECT … FOR UPDATE — 같은 대상에 대한 동시 실행을 줄 세운다
  2. 권한            역할 · 실행 주체 유형(human/agent) · 추가 조건(본인 담당 고객만 등)
  3. 실행 조건       상태 전이(20장) + 업무 규칙. 실패한 사유를 모두 돌려준다
                     expect 를 주면 대상이 읽은 뒤 바뀌지 않았는지도 확인한다
  4. 승인            required_if 가 참이면 실행하지 않고 승인 요청만 남긴다
  5. 변경            edits 를 순서대로 실행 (update · update_linked · create)
                     바꿀 행을 잠그고 다시 읽어 동시 변경을 감지하고, 상태 전이를 다시 확인한다
  6. 이벤트·감사     outbox 에 이벤트, action_log 에 실행 기록, state_transitions 에 상태 전이
어느 단계에서든 DB 오류가 나면 전체가 롤백되고, 실패 기록만 따로 남는다.
dry_run=True 이면 전 과정을 실행한 뒤 롤백한다 (AI 에이전트의 '실행 전 미리보기').
check()·available() 은 변경 없이 '지금 실행하면 어떻게 되는지'만 알려 준다 (20장).
21장: 대상이 실행자의 조회 범위(행 정책) 밖이면 denied. 승인 요청에는 변경 미리보기·기한·알림 이벤트가
붙고, 승인 후 실제 변경이 미리보기와 다르면 실행하지 않는다.
22장: 잠금 대기 시간(lock_timeout), 멱등 키(같은 요청은 저장된 응답을 재생), 오류 분류(retryable)와
자동 재시도(retries). 멱등 키는 변경과 같은 트랜잭션에 기록되므로 실패하면 키도 남지 않아 다시 시도할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .functions import FunctionError
from .metamodel import Action, Condition, Edit
from .policy import PolicyError
from .query import QueryError, coerce
from .states import StateMachines

if TYPE_CHECKING:
    from .objects import ObjectSet, OntologyClient, OntoObject

ENGINE_SCHEMA = "minionto"

# 기록 시각은 now()(트랜잭션 시작 시각)가 아니라 clock_timestamp()(실제 기록 순간)를 쓴다.
# 바깥 트랜잭션 하나에서 여러 Action 을 실행해도(체험 모드·테스트) 기록 순서가 시각에 남는다.
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {ENGINE_SCHEMA};
CREATE TABLE IF NOT EXISTS {ENGINE_SCHEMA}.action_log (
    execution_id uuid PRIMARY KEY,
    action       text NOT NULL,
    object_type  text NOT NULL,
    target_pk    text NOT NULL,
    actor        jsonb NOT NULL,
    params       jsonb NOT NULL,
    status       text NOT NULL,
    messages     jsonb NOT NULL DEFAULT '[]',
    changes      jsonb NOT NULL DEFAULT '[]',
    approval_id  uuid,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS {ENGINE_SCHEMA}.approval_requests (
    approval_id    uuid PRIMARY KEY,
    action         text NOT NULL,
    object_type    text NOT NULL,
    target_pk      text NOT NULL,
    requested_by   jsonb NOT NULL,
    params         jsonb NOT NULL,
    approver_roles jsonb NOT NULL,
    reason         text,
    status         text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_by     jsonb,
    decided_at     timestamptz,
    comment        text,
    execution_id   uuid,
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS {ENGINE_SCHEMA}.outbox (
    event_id     bigserial PRIMARY KEY,
    event_type   text NOT NULL,
    execution_id uuid NOT NULL,
    object_type  text NOT NULL,
    target_pk    text NOT NULL,
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz
);
-- 상태 전이 기록 (20장): 객체별 상태 이력
CREATE TABLE IF NOT EXISTS {ENGINE_SCHEMA}.state_transitions (
    transition_id bigserial PRIMARY KEY,
    execution_id  uuid NOT NULL,
    action        text NOT NULL,
    object_type   text NOT NULL,
    target_pk     text NOT NULL,
    property      text NOT NULL,
    from_state    text,
    to_state      text NOT NULL,
    actor         jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS state_transitions_object
    ON {ENGINE_SCHEMA}.state_transitions (object_type, target_pk);
-- 정책 판단 기록 (21장): 거부, 그리고 audit_reads 에 적은 실행자의 조회
CREATE TABLE IF NOT EXISTS {ENGINE_SCHEMA}.policy_decisions (
    decision_id  bigserial PRIMARY KEY,
    actor        jsonb NOT NULL,
    decision     text NOT NULL CHECK (decision IN ('allow', 'deny')),
    kind         text NOT NULL,
    object_type  text,
    target_pk    text,
    policy       text,
    detail       jsonb NOT NULL DEFAULT '{{}}',
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- 멱등 키 (22장): 같은 실행자가 같은 키로 보낸 요청은 한 번만 실행하고, 다시 오면 저장한 응답을 돌려준다
CREATE TABLE IF NOT EXISTS {ENGINE_SCHEMA}.idempotency_keys (
    actor_id         text NOT NULL,
    key              text NOT NULL,
    request_hash     text NOT NULL,
    action           text NOT NULL,
    target_pk        text NOT NULL,
    execution_id     uuid NOT NULL,
    response         jsonb,
    replays          integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_replayed_at timestamptz,
    PRIMARY KEY (actor_id, key)
);
-- 실패 분류 (22장)
ALTER TABLE {ENGINE_SCHEMA}.action_log ADD COLUMN IF NOT EXISTS error jsonb;
-- 승인 워크플로 (21장): 기한과 변경 미리보기
ALTER TABLE {ENGINE_SCHEMA}.approval_requests ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE {ENGINE_SCHEMA}.approval_requests ADD COLUMN IF NOT EXISTS preview jsonb NOT NULL DEFAULT '[]';
-- 이전 버전에서 만든 테이블도 같은 기본값을 쓰도록
ALTER TABLE {ENGINE_SCHEMA}.action_log        ALTER COLUMN created_at SET DEFAULT clock_timestamp();
ALTER TABLE {ENGINE_SCHEMA}.approval_requests ALTER COLUMN created_at SET DEFAULT clock_timestamp();
ALTER TABLE {ENGINE_SCHEMA}.outbox            ALTER COLUMN created_at SET DEFAULT clock_timestamp();
"""


def _json(value: Any) -> Jsonb:
    def default(v):
        if isinstance(v, (date, datetime, Decimal, uuid.UUID)):
            return str(v)
        if hasattr(v, "pk") and hasattr(v, "object_type"):     # OntoObject → 기본키
            return v.pk
        raise TypeError(type(v).__name__)
    return Jsonb(value, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=default))


class ActionStop(Exception):
    """실행을 멈추고 결과를 돌려준다 (DB 오류가 아닌 업무상 중단)"""
    def __init__(self, status: str, messages: list[str]):
        super().__init__("; ".join(messages))
        self.status, self.messages = status, messages


class StateConflict(Exception):
    """변경 도중 발견한 상태 문제 — 허용되지 않은 전이, 다른 트랜잭션의 동시 변경.
    이미 일부 변경을 했을 수 있으므로 ActionStop 과 달리 트랜잭션 전체를 롤백한다 (결과는 rejected).
    retryable: 동시 변경처럼 다시 시도하면 될 수 있는 경우 True (22장)"""
    def __init__(self, messages: list[str], retryable: bool = False):
        super().__init__("; ".join(messages))
        self.messages, self.retryable = messages, retryable


class _Replay(Exception):
    """같은 멱등 키의 요청 — 실행하지 않고 저장된 응답을 돌려준다"""
    def __init__(self, result: ActionResult):
        self.result = result


def classify(e: Exception) -> tuple[bool, str]:
    """시스템 오류 분류 (22장): (다시 시도하면 될 수 있는가, 오류 코드)

    - 잠금 시간 초과·교착 상태·직렬화 실패·연결 끊김 → 일시적 (retryable)
    - 제약 위반 등 데이터·정의 문제, 프로그램 오류 → 영구적 (같은 입력이면 다시 해도 같은 결과)
    """
    code = getattr(e, "sqlstate", None) or type(e).__name__
    return isinstance(e, psycopg.OperationalError), code


@dataclass
class ActionResult:
    status: str                 # completed | approval_required | rejected | denied | invalid | failed
    action: str
    object_type: str
    target: Any
    execution_id: str
    messages: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    approval_id: str | None = None
    dry_run: bool = False
    transitions: list[dict] = field(default_factory=list)     # 20장: 이 실행이 일으킨 상태 전이
    retryable: bool = False                 # 22장: 다시 시도하면 될 수 있는 실패인가
    error_code: str | None = None           # 22장: SQLSTATE 또는 오류 종류
    attempts: int = 1                       # 22장: 시도 횟수 (retries)
    replayed: bool = False                  # 22장: 멱등 키로 저장된 응답을 돌려준 것인가

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict:
        """AI 에이전트·API 응답용 (JSON 직렬화 가능)"""
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, default=str))

    def __str__(self) -> str:
        head = f"[{self.status}{' · dry-run' if self.dry_run else ''}] {self.action}({self.target})"
        lines = [head] + [f"  - {m}" for m in self.messages]
        for c in self.changes:
            after = ", ".join(f"{k}={v}" for k, v in c["after"].items() if k != "created_at")
            lines.append(f"  * {c['op']} {c['object_type']}({c['pk']}) {after}")
        if self.events:
            lines.append(f"  → 이벤트 {', '.join(self.events)}")
        return "\n".join(lines)


@dataclass
class ActionCheck:
    """check() 결과 — 실행하지 않고 판단만 한다"""
    action: str
    display_name: str
    status: str                 # ready | approval_required | needs_input | rejected | denied | invalid
    messages: list[str] = field(default_factory=list)
    next_state: str | None = None
    missing: list[str] = field(default_factory=list)      # 아직 받지 못한 필수 입력

    @property
    def ok(self) -> bool:
        """지금 실행할 수 있는가 (승인 요청으로 이어지는 경우 포함)"""
        return self.status in ("ready", "approval_required")

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        nxt = f" → {self.next_state}" if self.next_state else ""
        lines = [f"{self.status:17s} {self.action}{nxt}"]
        return "\n".join(lines + [f"{'':19s}- {m}" for m in self.messages])


@dataclass
class Availability:
    """available() 결과 — 한 객체에 대해 지금 할 수 있는 일"""
    object_type: str
    pk: Any
    state: dict | None           # {property, value, terminal, next: {action: 다음 상태}}
    actions: list[ActionCheck]

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, default=str))

    def __str__(self) -> str:
        head = f"{self.object_type}({self.pk})"
        if self.state:
            head += f" — {self.state['property']}: {self.state['value']}" + \
                (" (종료 상태)" if self.state["terminal"] else "")
        return "\n".join([head] + [str(c) for c in self.actions])


SYSTEM = {"id": "minionto-scheduler", "role": "시스템", "type": "system"}


def _duration(text: str) -> timedelta:
    """'30m' · '24h' · '3d' → timedelta"""
    m = re.fullmatch(r"([1-9][0-9]*)([mhd])", text)
    if not m:
        raise QueryError(f"기한 형식 오류: {text} (예: 30m, 24h, 3d)")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def _minute(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _uses_params(c: Condition | None, names: list[str]) -> bool:
    """조건식이 아직 받지 못한 파라미터($params.x)를 참조하는가"""
    if c is None or not names:
        return False
    text = c.model_dump_json(by_alias=True)
    return any(f"$params.{n}" in text for n in names)


def _has_function(c: Condition | None) -> bool:
    if c is None:
        return False
    if c.function is not None:
        return True
    children = (c.all_ or []) + (c.any_ or []) + ([c.not_] if c.not_ else [])
    return any(_has_function(x) for x in children)


class ActionEngine:
    def __init__(self, client: OntologyClient):
        self.client = client
        self.states = StateMachines(client.onto)
        self._ready = False
        self.lock_timeout = "5s"            # 22장: 다른 실행이 잡은 잠금을 이 시간까지만 기다린다
        self.retry_backoff = 0.2            # 22장: 재시도 간격(초), 시도마다 두 배

    @property
    def conn(self):
        return self.client.conn

    def ensure_tables(self) -> None:
        """엔진 전용 테이블을 만든다. 체험 모드처럼 롤백할 트랜잭션 안에서 처음 불려도 테이블이 남도록
        별도의 짧은 연결(autocommit)에서 만든다. 최신 테이블이 이미 있으면 아무것도 하지 않는다."""
        if self._ready:
            return
        import psycopg

        from .db import database_url
        with psycopg.connect(database_url(self.client.onto, self.client._url), autocommit=True) as ddl:
            current = ddl.execute(
                f"""SELECT to_regclass('{ENGINE_SCHEMA}.idempotency_keys') IS NOT NULL AND EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_schema = '{ENGINE_SCHEMA}'
                        AND table_name = 'action_log' AND column_name = 'error')""").fetchone()[0]
            if not current:
                ddl.execute("SET lock_timeout = '10s'")
                ddl.execute(DDL)
        self._ready = True

    # ================================================================== 실행
    def apply(self, action: str, target: Any, *, actor: dict, params: dict | None = None,
              dry_run: bool = False, expect: dict | None = None, idempotency_key: str | None = None,
              retries: int = 0, _approval: str | None = None, _preview: list | None = None) -> ActionResult:
        """expect: {"status": "검토중"} 처럼 실행자가 읽어 둔 값. 그사이 바뀌었으면 rejected (20장)
        idempotency_key: 같은 실행자가 같은 키로 다시 보내면 실행하지 않고 처음 응답을 돌려준다 (22장)
        retries: retryable 실패(잠금 시간 초과·동시 변경 등)일 때 처음부터 다시 실행할 횟수 (22장)"""
        attempt = 0
        while True:
            attempt += 1
            res = self._apply_once(action, target, actor=actor, params=params, dry_run=dry_run, expect=expect,
                                   idempotency_key=idempotency_key, _approval=_approval, _preview=_preview)
            res.attempts = attempt
            if not res.retryable or attempt > retries:
                return res
            time.sleep(self.retry_backoff * 2 ** (attempt - 1))

    def _apply_once(self, action: str, target: Any, *, actor: dict, params: dict | None, dry_run: bool,
                    expect: dict | None, idempotency_key: str | None, _approval: str | None,
                    _preview: list | None) -> ActionResult:
        self.ensure_tables()
        act = self.client.onto.actions.get(action)
        if act is None:
            raise QueryError(f"Action '{action}' 이 없습니다 (있는 Action: {', '.join(self.client.onto.actions)})")
        if hasattr(target, "pk"):
            target = target.pk
        res = ActionResult("completed", action, act.target, target, str(uuid.uuid4()), dry_run=dry_run,
                           approval_id=_approval)
        actor = dict(actor)
        params_in = dict(params or {})
        key = idempotency_key if (idempotency_key and not dry_run) else None
        try:
            with self.conn.transaction(force_rollback=dry_run):
                # 잠금을 무한정 기다리지 않는다 (이 트랜잭션 안에서만 적용)
                self.conn.execute("SELECT set_config('lock_timeout', %s, true)", (self.lock_timeout,))
                replay, owns_key = None, False
                try:
                    actor = self._check_actor(actor)
                    if key:
                        self._claim_key(key, action, target, params_in, expect, actor, res)
                        owns_key = True                          # 선점에 성공했을 때만 응답을 저장한다
                    self._check_role(act, actor)                 # 권한 없는 실행자에게는 입력 오류도 알려주지 않는다
                    params_ = self._check_params(act, params_in)
                    target_set = self._lock_target(act, target, params_, actor)
                    self._check_visible(act, action, target, actor)
                    self._check_permission(act, target_set, params_, actor)
                    if expect:
                        self._check_expect(target_set, expect)
                    self._check_preconditions(action, act, target_set, params_, actor)
                    if _approval is None and act.approval and \
                            self.evaluate(act.approval.required_if, target_set, params_, actor):
                        preview = self._preview(act, target_set, params_, actor, res)
                        res.approval_id, expires, existing = self._request_approval(
                            act, action, target, actor, params_, preview, res)
                        res.events = [] if existing else ["approval_requested"]
                        raise ActionStop("approval_required",
                                         [act.approval.reason or "승인이 필요합니다",
                                          f"승인자 역할: {', '.join(act.approval.approver_roles)}"]
                                         + ([f"승인 기한: {_minute(expires)}"] if expires else [])
                                         + ([f"같은 승인 요청이 이미 대기 중입니다 (요청자 {existing}) — "
                                             "새로 만들지 않았습니다"] if existing else
                                            [f"변경 미리보기 {len(preview)}건을 승인 요청에 저장했습니다"]))
                    for edit in act.edits:
                        res.changes += self._apply_edit(act, edit, target_set, params_, actor, res)
                    if _preview is not None:
                        self._check_preview(_preview, res.changes)
                    self._record_transitions(res, actor)
                    res.events = self._emit(act, res, target, params_, actor)
                    res.messages.append(f"{len(res.changes)}건 변경" + (" (dry-run: 롤백됨)" if dry_run else ""))
                except ActionStop as stop:
                    res.status, res.messages = stop.status, stop.messages
                except _Replay as r:
                    replay = r.result
                if replay is not None:
                    return replay
                self._log(res, actor, params_in)
                if owns_key:
                    self._store_key(key, actor, res)
        except Exception as e:  # noqa: BLE001 — 트랜잭션은 이미 롤백됨(멱등 키 포함). 실패 기록만 따로 남긴다
            if isinstance(e, StateConflict):
                res.status, res.messages = "rejected", e.messages        # 허용되지 않은 전이·동시 변경
                res.retryable, res.error_code = e.retryable, "state_conflict"
            elif isinstance(e, (FunctionError, QueryError)):
                res.status, res.messages = "invalid", [str(e)]           # 정의·입력 문제
            else:
                res.retryable, res.error_code = classify(e)
                kind = "일시적 오류 — 다시 시도할 수 있습니다" if res.retryable else "다시 시도해도 같은 결과입니다"
                res.status, res.messages = "failed", [f"{type(e).__name__}: {e}".strip(), f"({kind})"]
            res.changes, res.events, res.transitions = [], [], []
            if not dry_run:
                try:
                    with self.conn.transaction():
                        self._log(res, actor, params_in)
                except psycopg.Error:                                    # 연결이 끊긴 경우 등: 기록도 실패
                    res.messages.append("(실패 기록을 남기지 못했습니다)")
        return res

    # ------------------------------------------------------------------ 멱등 키 (22장)
    @staticmethod
    def _request_hash(action: str, target: Any, params: dict, expect: dict | None) -> str:
        body = json.dumps({"action": action, "target": str(target), "params": params, "expect": expect},
                          ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    def _claim_key(self, key: str, action: str, target: Any, params: dict, expect: dict | None, actor: dict,
                   res: ActionResult) -> None:
        """키를 선점한다. 이미 있으면(같은 요청의 재전송) _Replay, 다른 요청이면 invalid.
        같은 키로 동시에 들어온 두 번째 요청은 INSERT 에서 첫 트랜잭션이 끝나기를 기다린다."""
        h = self._request_hash(action, target, params, expect)
        claimed = self.conn.execute(
            f"""INSERT INTO {ENGINE_SCHEMA}.idempotency_keys (actor_id, key, request_hash, action, target_pk, execution_id)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (actor_id, key) DO NOTHING RETURNING key""",
            (actor["id"], key, h, action, str(target), res.execution_id)).fetchone()
        if claimed:
            return
        prev = self.conn.execute(
            f"SELECT * FROM {ENGINE_SCHEMA}.idempotency_keys WHERE actor_id = %s AND key = %s FOR UPDATE",
            (actor["id"], key)).fetchone()
        if prev["request_hash"] != h:
            raise ActionStop("invalid", [f"멱등 키 '{key}' 는 이미 다른 요청({prev['action']}({prev['target_pk']}))에 "
                                         "쓰였습니다. 새 요청에는 새 키를 쓰세요"])
        self.conn.execute(
            f"""UPDATE {ENGINE_SCHEMA}.idempotency_keys SET replays = replays + 1, last_replayed_at = clock_timestamp()
                WHERE actor_id = %s AND key = %s""", (actor["id"], key))
        known = {f.name for f in fields(ActionResult)}
        stored = ActionResult(**{k: v for k, v in prev["response"].items() if k in known})
        stored.replayed = True
        raise _Replay(stored)

    def _store_key(self, key: str, actor: dict, res: ActionResult) -> None:
        self.conn.execute(
            f"UPDATE {ENGINE_SCHEMA}.idempotency_keys SET response = %s WHERE actor_id = %s AND key = %s",
            (_json(res.to_dict()), actor["id"], key))

    def purge_idempotency_keys(self, older_than: str = "1d") -> int:
        """오래된 멱등 키 정리 — 클라이언트의 재시도 기간보다 길게 보관한다"""
        self.ensure_tables()
        cur = self.conn.execute(f"DELETE FROM {ENGINE_SCHEMA}.idempotency_keys WHERE created_at < now() - %s::interval",
                                (_duration(older_than),))
        return cur.rowcount

    def candidates(self, action: str, *, actor: dict | None = None, params: dict | None = None) -> ObjectSet:
        """이 Action 을 실행할 수 있는 대상 객체 집합

        실행 조건(과 actor 를 주면 권한 조건)을 그대로 조회 조건으로 쓴다. 규칙이 한곳(YAML)에만
        있으므로 '실행 가능 목록'과 '실행 시 검사'가 어긋나지 않는다. Function 조건은 SQL 로
        바꿀 수 없어 제외되므로, 최종 판단은 apply() 가 한다.
        """
        act = self.client.onto.actions[action]
        q = self.client.objects(act.target).bind(**(params or {}))
        rule = self.states.rule(act.target, action)
        if rule:                                                # 20장: 시작 상태도 조회 조건으로
            q = q.where({"field": rule.property, "op": "in", "value": list(rule.from_)})
        if actor is not None:
            q = q.secure(actor)                                 # 21장: 실행자에게 보이는 대상만
            perm = act.permission
            if actor.get("role") not in perm.roles or actor.get("type") not in perm.actor_types:
                return q.where({"any": []})                     # 권한 없음 → 빈 집합
            if act.permission.check and not _has_function(act.permission.check):
                q = q.where(act.permission.check)
        for rule in act.preconditions:
            if not _has_function(rule.check):
                q = q.where(rule.check)
        return q

    def check(self, action: str, target: Any, *, actor: dict, params: dict | None = None) -> ActionCheck:
        """지금 이 대상에 action 을 실행하면 어떻게 되는지 — 아무것도 바꾸지 않고, 기록도 남기지 않는다.

        apply() 와 같은 순서로 검사한다. 필수 입력이 빠졌으면 그 입력을 쓰지 않는 조건까지만
        검사하고 needs_input 을 돌려준다 (AI 에이전트가 무엇을 더 물어야 하는지 알 수 있다).
        """
        act = self.client.onto.actions.get(action)
        if act is None:
            raise QueryError(f"Action '{action}' 이 없습니다")
        if hasattr(target, "pk"):
            target = target.pk
        rule = self.states.rule(act.target, action)
        out = ActionCheck(action, act.display_name, "ready", next_state=rule.to if rule else None)
        given = dict(params or {})
        try:
            actor = self._check_actor(dict(actor))
            self._check_role(act, actor)
            missing = [n for n, p in act.parameters.items() if p.required and p.default is None and n not in given]
            params_ = self._check_params(act, given, partial=True)
            ts = self._target_set(act, target, params_, actor, lock=False)
            self._check_visible(act, action, target, actor)
            if not _uses_params(act.permission.check, missing):
                self._check_permission(act, ts, params_, actor)
            failed = self._failed_rules(action, act, ts, params_, actor, skip=missing)
            if failed:
                raise ActionStop("rejected", failed)
            if missing:
                out.missing = missing
                raise ActionStop("needs_input", [f"입력이 필요합니다: {', '.join(missing)} "
                                                 "(입력을 받으면 그 입력을 쓰는 조건을 마저 검사합니다)"])
            if act.approval and not _uses_params(act.approval.required_if, missing) and \
                    self.evaluate(act.approval.required_if, ts, params_, actor):
                raise ActionStop("approval_required", [act.approval.reason or "승인이 필요합니다",
                                                       f"승인자 역할: {', '.join(act.approval.approver_roles)}"])
        except ActionStop as stop:
            out.status, out.messages = stop.status, stop.messages
        except (FunctionError, QueryError) as e:
            out.status, out.messages = "invalid", [str(e)]
        return out

    def available(self, object_type: str, pk: Any, *, actor: dict) -> Availability:
        """한 객체에 대해 지금 할 수 있는 일 — 이 타입을 대상으로 하는 모든 Action 을 check() 한다"""
        if object_type not in self.client.onto.object_types:
            raise QueryError(f"객체 타입 '{object_type}' 이 없습니다")
        obj = self.client.get(object_type, pk, actor=actor)              # 21장: 보이는 객체만
        if obj is None:
            raise QueryError(f"{object_type} '{pk}' 가 없습니다")
        state = None
        m = self.states.machine(object_type)
        if m:
            value = obj.properties.get(m.property)
            state = {"property": m.property, "value": value, "terminal": value in m.terminal,
                     "next": self.states.next_actions(object_type, value)}
        checks = [self.check(name, obj.pk, actor=actor)
                  for name, act in self.client.onto.actions.items() if act.target == object_type]
        return Availability(object_type, obj.pk, state, checks)

    def history(self, object_type: str, pk: Any) -> list[dict]:
        """객체의 상태 전이 이력 (오래된 것부터)"""
        self.ensure_tables()
        return self.conn.execute(
            f"""SELECT t.created_at, t.property, t.from_state, t.to_state, t.action,
                       t.actor->>'id' AS actor_id, t.actor->>'type' AS actor_type, t.execution_id
                FROM {ENGINE_SCHEMA}.state_transitions t
                WHERE t.object_type = %s AND t.target_pk = %s
                ORDER BY t.created_at, t.transition_id""", (object_type, str(pk))).fetchall()

    # ------------------------------------------------------------------ 0~3단계: 검사
    @staticmethod
    def _check_actor(actor: dict) -> dict:
        missing = {"id", "role", "type"} - set(actor)
        if missing:
            raise ActionStop("invalid", [f"실행자 정보에 {sorted(missing)} 가 없습니다"])
        if actor["type"] not in ("human", "agent", "system"):
            raise ActionStop("invalid", [f"실행자 유형 '{actor['type']}' 은 human·agent·system 중 하나여야 합니다"])
        return actor

    def _check_params(self, act: Action, given: dict, partial: bool = False) -> dict:
        unknown = set(given) - set(act.parameters)
        if unknown:
            raise ActionStop("invalid", [f"정의에 없는 파라미터: {', '.join(sorted(unknown))}"])
        out: dict[str, Any] = {}
        fe = self.client.functions
        for name, p in act.parameters.items():
            value = given.get(name, p.default)
            if value is None:
                if p.required and not partial:
                    raise ActionStop("invalid", [f"필수 파라미터 '{name}' 이 없습니다"])
                out[name] = None
                continue
            try:
                v = fe.to_input(p, value, f"parameters.{name}")
            except FunctionError as e:
                raise ActionStop("invalid", [str(e)]) from None
            out[name] = v.pk if hasattr(v, "pk") and hasattr(v, "object_type") else v
        return out

    def _lock_target(self, act: Action, target: Any, params: dict, actor: dict) -> ObjectSet:
        return self._target_set(act, target, params, actor, lock=True)

    def _target_set(self, act: Action, target: Any, params: dict, actor: dict, *, lock: bool) -> ObjectSet:
        ot = self.client.onto.object_types[act.target]
        pk_prop = ot.properties[ot.source.primary_key]
        pk = coerce(pk_prop, target, "target")
        row = self.conn.execute(
            sql.SQL("SELECT 1 FROM {} WHERE {} = %s" + (" FOR UPDATE" if lock else "")).format(
                sql.Identifier(ot.source.table), sql.Identifier(pk_prop.column or ot.source.primary_key)),
            (pk,)).fetchone()
        if row is None:
            raise ActionStop("invalid", [f"{act.target} '{target}' 가 없습니다"])
        return (self.client.objects(act.target).where(**{ot.source.primary_key: pk})
                .bind(**params).as_actor(actor))

    @staticmethod
    def _check_role(act: Action, actor: dict) -> None:
        perm = act.permission
        if actor["role"] not in perm.roles:
            raise ActionStop("denied", [f"역할 '{actor['role']}' 은 이 Action 을 실행할 수 없습니다 "
                                        f"(허용: {', '.join(perm.roles)})"])
        if actor["type"] not in perm.actor_types:
            raise ActionStop("denied", [f"'{actor['type']}' 실행자는 이 Action 을 실행할 수 없습니다 "
                                        f"(허용: {', '.join(perm.actor_types)})"])

    def _check_visible(self, act: Action, action: str, target: Any, actor: dict) -> None:
        """조회할 수 없는 객체는 바꿀 수도 없다 (행 정책, 21장)"""
        try:
            self.client.policy.require_visible(act.target, target, actor, f"Action {action}")
        except PolicyError as e:
            raise ActionStop("denied", [str(e)]) from None

    def _check_permission(self, act: Action, ts: ObjectSet, params: dict, actor: dict) -> None:
        perm = act.permission
        if perm.check and not self.evaluate(perm.check, ts, params, actor):
            raise ActionStop("denied", ["권한 조건을 만족하지 않습니다 (예: 본인 담당 고객이 아님)"])

    def _check_preconditions(self, action: str, act: Action, ts: ObjectSet, params: dict, actor: dict) -> None:
        failed = self._failed_rules(action, act, ts, params, actor)
        if failed:
            raise ActionStop("rejected", failed)

    def _failed_rules(self, action: str, act: Action, ts: ObjectSet, params: dict, actor: dict,
                      skip: list[str] | None = None) -> list[str]:
        """상태 전이 + 실행 조건 중 만족하지 못한 것의 메시지를 모두 (하나만이 아니라)"""
        failed = []
        rule = self.states.rule(act.target, action)
        if rule:
            current = ts.rows(rule.property)[0][rule.property]
            if current not in rule.from_:
                failed.append(f"현재 상태가 '{current}' 입니다. '{act.display_name}' 은 "
                              f"{'·'.join(rule.from_)} 상태에서만 할 수 있습니다.")
        failed += [r.message for r in act.preconditions
                   if not _uses_params(r.check, skip or []) and not self.evaluate(r.check, ts, params, actor)]
        return failed

    def _check_expect(self, ts: ObjectSet, expect: dict) -> None:
        """낙관적 동시성 검사: 실행자가 읽은 값과 지금 값이 다르면 멈춘다"""
        current = ts.rows(*expect)[0]
        changed = [f"대상이 그사이 바뀌었습니다: {k} 예상 '{v}', 현재 '{current[k]}'"
                   for k, v in expect.items() if str(current[k]) != str(v)]
        if changed:
            raise ActionStop("rejected", changed + ["최신 상태를 다시 읽고 판단하세요."])

    def evaluate(self, cond: Condition, ts: ObjectSet, params: dict, actor: dict) -> bool:
        """조건식 평가: Function 이 없으면 SQL 한 번, 있으면 논리 연산을 Python 에서 나눠 평가"""
        if not _has_function(cond):
            return ts.where(cond).exists()
        if cond.all_ is not None:
            return all(self.evaluate(c, ts, params, actor) for c in cond.all_)
        if cond.any_ is not None:
            return any(self.evaluate(c, ts, params, actor) for c in cond.any_)
        if cond.not_ is not None:
            return not self.evaluate(cond.not_, ts, params, actor)
        args = {k: self.resolve(v, ts, params, actor) for k, v in (cond.args or {}).items()}
        result = self.client.functions.call(cond.function, **args)
        if not isinstance(result, bool):
            raise FunctionError(f"조건으로 쓴 Function '{cond.function}' 이 boolean 을 돌려주지 않았습니다")
        return result

    def resolve(self, raw: Any, ts: ObjectSet, params: dict, actor: dict) -> Any:
        """edit 값·Function 인자의 $참조를 실제 값으로"""
        if not (isinstance(raw, str) and raw.startswith("$")):
            return raw
        if raw == "$today":
            return self.client.today or date.today()
        if raw == "$now":
            return self.client.now()
        head, _, name = raw.partition(".")
        if head == "$params":
            return params.get(name)
        if head == "$actor":
            return actor.get(name)
        if head == "$target":
            return ts.rows(name)[0][name]
        raise QueryError(f"알 수 없는 참조 '{raw}'")

    # ------------------------------------------------------------------ 4단계: 승인 요청
    def _preview(self, act: Action, ts: ObjectSet, params: dict, actor: dict, res: ActionResult) -> list[dict]:
        """승인자가 볼 변경 미리보기: edits 를 실제로 실행한 뒤 이 부분만 롤백한다 (dry-run 과 같은 방식)"""
        tmp = ActionResult("completed", res.action, act.target, res.target, res.execution_id)
        with self.conn.transaction(force_rollback=True):
            for edit in act.edits:
                tmp.changes += self._apply_edit(act, edit, ts, params, actor, tmp)
        return json.loads(json.dumps(tmp.changes, ensure_ascii=False, default=str))

    def _fingerprint(self, changes: list[dict]) -> list[str]:
        """미리보기와 실제 변경을 비교하는 형태: 실행 시각(datetime)과 새 객체의 기본키는 빼고 비교"""
        out = []
        for c in json.loads(json.dumps(changes, ensure_ascii=False, default=str)):
            ot = self.client.onto.object_types[c["object_type"]]
            skip = {ot.source.primary_key} | {n for n, p in ot.properties.items() if p.type == "datetime"}
            item = {"op": c["op"], "type": c["object_type"],
                    "after": {k: v for k, v in c["after"].items() if k not in skip}}
            if c["op"] == "update":
                item["pk"] = c["pk"]
                item["before"] = {k: v for k, v in (c.get("before") or {}).items() if k not in skip}
            out.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return sorted(out)

    def _check_preview(self, preview: list[dict], changes: list[dict]) -> None:
        """승인자가 본 것과 실제로 바뀌는 것이 같아야 한다 (21장)"""
        seen, now = self._fingerprint(preview), self._fingerprint(changes)
        if seen == now:
            return

        def label(item: str) -> str:
            d = json.loads(item)
            return f"{d['op']} {d['type']}" + (f"({d['pk']})" if "pk" in d else "")
        extra = [label(x) for x in now if x not in seen]
        missing = [label(x) for x in seen if x not in now]
        raise StateConflict([f"승인한 변경 내용과 실제 변경이 다릅니다 (미리보기 {len(preview)}건, 지금 {len(changes)}건)"]
                            + [f"미리보기에 없던 변경: {x}" for x in extra[:3]]
                            + [f"미리보기에 있었지만 사라진 변경: {x}" for x in missing[:3]])

    def _request_approval(self, act: Action, action: str, target: Any, actor: dict, params: dict,
                          preview: list[dict], res: ActionResult) -> tuple[str, datetime | None, str | None]:
        """(approval_id, 기한, 이미 있던 요청의 요청자). 같은 대상·같은 입력의 승인 요청이 기한 안에서
        대기 중이면 새로 만들지 않고 그것을 돌려준다 (22장: 업무 규칙에 의한 중복 방지)"""
        dup = self.conn.execute(
            f"""SELECT approval_id, expires_at, requested_by FROM {ENGINE_SCHEMA}.approval_requests
                WHERE status = 'pending' AND action = %s AND target_pk = %s AND params = %s
                  AND (expires_at IS NULL OR expires_at >= %s)
                ORDER BY created_at LIMIT 1""",
            (action, str(target), _json(params), self.client.now())).fetchone()
        if dup:
            exp = dup["expires_at"].replace(tzinfo=None) if dup["expires_at"] else None
            return str(dup["approval_id"]), exp, dup["requested_by"].get("id")
        approval_id = str(uuid.uuid4())
        expires = self.client.now() + _duration(act.approval.expires_in) if act.approval.expires_in else None
        self.conn.execute(
            f"""INSERT INTO {ENGINE_SCHEMA}.approval_requests
                (approval_id, action, object_type, target_pk, requested_by, params, approver_roles, reason, status,
                 expires_at, preview)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)""",
            (approval_id, action, act.target, str(target), _json(actor), _json(params),
             _json(act.approval.approver_roles), act.approval.reason, expires, _json(preview)))
        # 승인자에게 알리는 이벤트 — 요청과 같은 트랜잭션에 outbox 로 (실제 발송은 23장)
        self._event("approval_requested", res.execution_id, act.target, target,
                    {"approval_id": approval_id, "action": action, "requested_by": actor,
                     "approver_roles": act.approval.approver_roles, "reason": act.approval.reason,
                     "expires_at": expires, "changes": len(preview)})
        return approval_id, expires, None

    def _event(self, event_type: str, execution_id: str, object_type: str, target_pk: Any, payload: dict) -> None:
        self.conn.execute(
            f"""INSERT INTO {ENGINE_SCHEMA}.outbox (event_type, execution_id, object_type, target_pk, payload)
                VALUES (%s, %s, %s, %s, %s)""", (event_type, execution_id, object_type, str(target_pk), _json(payload)))

    def expire_stale(self, actor: dict | None = None) -> list[str]:
        """기한이 지난 대기 요청을 만료로 닫는다 (스케줄러가 주기적으로 부르는 시스템 작업)"""
        self.ensure_tables()
        actor = actor or SYSTEM
        closed = []
        with self.conn.transaction():
            rows = self.conn.execute(
                f"""SELECT approval_id, action, object_type, target_pk, expires_at FROM {ENGINE_SCHEMA}.approval_requests
                    WHERE status = 'pending' AND expires_at < %s ORDER BY expires_at FOR UPDATE SKIP LOCKED""",
                (self.client.now(),)).fetchall()
            for r in rows:
                self._close_approval(str(r["approval_id"]), "expired", actor, "기한 초과", None)
                self._event("approval_expired", str(uuid.uuid4()), r["object_type"], r["target_pk"],
                            {"approval_id": str(r["approval_id"]), "action": r["action"],
                             "expires_at": r["expires_at"], "reason": "기한 초과"})
                closed.append(str(r["approval_id"]))
        return closed

    def _close_approval(self, approval_id: str, status: str, actor: dict, comment: str | None,
                        execution_id: str | None) -> None:
        self.conn.execute(
            f"""UPDATE {ENGINE_SCHEMA}.approval_requests
                SET status = %s, decided_by = %s, decided_at = clock_timestamp(), comment = %s, execution_id = %s
                WHERE approval_id = %s""",
            (status, _json(actor), comment, execution_id, approval_id))

    def pending_approvals(self) -> list[dict]:
        self.ensure_tables()
        return self.conn.execute(
            f"""SELECT *, (expires_at IS NOT NULL AND expires_at < %s) AS overdue
                FROM {ENGINE_SCHEMA}.approval_requests WHERE status = 'pending' ORDER BY created_at""",
            (self.client.now(),)).fetchall()

    def decide(self, approval_id: str, approver: dict, approve: bool = True,
               comment: str | None = None) -> ActionResult:
        """승인 요청 처리. 승인하면 '요청한 실행자'의 권한으로 Action 을 다시 검사·실행한다."""
        self.ensure_tables()
        with self.conn.transaction():
            req = self.conn.execute(
                f"SELECT * FROM {ENGINE_SCHEMA}.approval_requests WHERE approval_id = %s FOR UPDATE",
                (approval_id,)).fetchone()
            if req is None or req["status"] != "pending":
                raise QueryError(f"처리할 수 있는 승인 요청이 아닙니다: {approval_id}"
                                 + (f" (상태: {req['status']})" if req else ""))
            stop = None
            if approver.get("role") not in req["approver_roles"]:
                stop = f"역할 '{approver.get('role')}' 은 승인할 수 없습니다 (승인자: {', '.join(req['approver_roles'])})"
            elif approver.get("type") != "human":
                stop = "승인은 사람만 할 수 있습니다"
            elif approver.get("id") == req["requested_by"].get("id"):
                stop = "요청자 본인은 승인할 수 없습니다 (직무 분리)"
            if stop:
                return ActionResult("denied", req["action"], req["object_type"], req["target_pk"],
                                    "", [stop], approval_id=approval_id)

            def decided(decision: str, execution_id: str | None, messages: list[str]) -> None:
                self._event("approval_decided", execution_id or str(uuid.uuid4()), req["object_type"],
                            req["target_pk"], {"approval_id": approval_id, "action": req["action"],
                                               "decision": decision, "decided_by": approver,
                                               "requested_by": req["requested_by"], "messages": messages})

            expires = req.get("expires_at")
            if expires is not None and expires.replace(tzinfo=None) < self.client.now():   # 기한 초과
                self._close_approval(approval_id, "expired", approver, "기한 초과", None)
                msg = [f"승인 기한({_minute(expires.replace(tzinfo=None))})이 지나 요청이 만료되었습니다. "
                       "필요하면 요청자가 다시 요청해야 합니다."]
                decided("expired", None, msg)
                return ActionResult("rejected", req["action"], req["object_type"], req["target_pk"], "", msg,
                                    approval_id=approval_id)
            if not approve:
                self._close_approval(approval_id, "rejected", approver, comment, None)
                msg = [f"승인자가 반려했습니다{': ' + comment if comment else ''}"]
                decided("rejected", None, msg)
                return ActionResult("rejected", req["action"], req["object_type"], req["target_pk"], "", msg,
                                    approval_id=approval_id)
            result = self.apply(req["action"], req["target_pk"], actor=req["requested_by"],
                                params=req["params"], _approval=approval_id, _preview=req["preview"])
            # 요청 이후 상황이 바뀌어(예: 다른 사람이 이미 처리) 실행 조건을 못 맞추거나,
            # 실제 변경이 승인자가 본 미리보기와 다르면 '만료'
            status = "approved" if result.ok else "expired"
            self._close_approval(approval_id, status, approver, comment, result.execution_id)
            decided(status, result.execution_id, result.messages)
            return result

    # ------------------------------------------------------------------ 5단계: 변경
    def _apply_edit(self, act: Action, edit: Edit, ts: ObjectSet, params: dict, actor: dict,
                    res: ActionResult) -> list[dict]:
        if edit.update is not None:
            obj = ts.one()
            return self._update(act.target, [obj], self._values(act.target, edit.set, ts, params, actor), res)
        if edit.update_linked is not None:
            linked = ts.pivot(edit.update_linked)
            if edit.where is not None:
                linked = linked.where(edit.where)
            objs = linked.all()
            if not objs:
                return []
            return self._update(linked.object_type, objs,
                                self._values(linked.object_type, edit.set, ts, params, actor), res)
        return [self._create(edit.create, edit.values, ts, params, actor, res)]

    def _values(self, otype: str, raw: dict, ts: ObjectSet, params: dict, actor: dict) -> dict:
        props = self.client.onto.object_types[otype].properties
        out = {}
        for name, v in raw.items():
            value = self.resolve(v, ts, params, actor)
            out[name] = coerce(props[name], value, f"{otype}.{name}")
        return out

    def _update(self, otype: str, objs: list[OntoObject], values: dict, res: ActionResult) -> list[dict]:
        ot = self.client.onto.object_types[otype]
        pk_name = ot.source.primary_key
        pk_col = ot.properties[pk_name].column or pk_name

        # 동시 변경 감지: 바꿀 행을 잠그고 다시 읽어, 앞에서 읽은 값(before)과 같은지 확인한다.
        # 대상 객체는 1단계에서 잠갔지만 링크로 이어진 객체는 여기서 처음 잠근다.
        cols = sql.SQL(", ").join(sql.SQL("{} AS {}").format(
            sql.Identifier(ot.properties[k].column or k), sql.Identifier(k)) for k in [pk_name, *values])
        locked = {r[pk_name]: r for r in self.conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE {} = ANY(%s) ORDER BY {} FOR UPDATE").format(   # 22장: 잠금 순서 고정
                cols, sql.Identifier(ot.source.table), sql.Identifier(pk_col), sql.Identifier(pk_col)),
            ([o.pk for o in objs],))}
        conflicts = []
        for o in objs:
            now = locked.get(o.pk)
            if now is None:
                conflicts.append(f"동시 변경 감지: {otype}({o.pk}) 가 그사이 삭제되었습니다")
                continue
            conflicts += [f"동시 변경 감지: {otype}({o.pk}) 의 {k} 가 그사이 '{o.properties.get(k)}' → "
                          f"'{now[k]}' 로 바뀌었습니다" for k in values if now[k] != o.properties.get(k)]
        if conflicts:
            raise StateConflict(conflicts + ["다른 실행과 겹쳤습니다. 최신 상태로 다시 시도하세요."], retryable=True)

        # 상태 전이 검사: 실행 전 검사는 대상만 보았으므로, 링크 객체까지 모든 변경을 다시 확인한다
        m = self.states.machine(otype)
        if m and m.property in values:
            rule = self.states.rule(otype, res.action)
            after = values[m.property]
            for o in objs:
                before = o.properties.get(m.property)
                if rule is None or before not in rule.from_ or after != rule.to:
                    raise StateConflict([f"허용되지 않은 상태 전이: {otype}({o.pk}) {before} → {after} "
                                         f"({res.action})"])
                res.transitions.append({"object_type": otype, "pk": o.pk, "property": m.property,
                                        "from": before, "to": after})
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(ot.properties[k].column or k)) for k in values)
        self.conn.execute(
            sql.SQL("UPDATE {} SET {} WHERE {} = ANY(%s)").format(
                sql.Identifier(ot.source.table), assignments, sql.Identifier(pk_col)),
            (*values.values(), [o.pk for o in objs]))
        return [{"op": "update", "object_type": otype, "pk": o.pk,
                 "before": {k: o.properties.get(k) for k in values}, "after": values} for o in objs]

    def _create(self, otype: str, raw: dict, ts: ObjectSet, params: dict, actor: dict, res: ActionResult) -> dict:
        ot = self.client.onto.object_types[otype]
        values = {k: v for k, v in self._values(otype, raw, ts, params, actor).items() if v is not None}
        m = self.states.machine(otype)
        if m and values.get(m.property) not in m.initial:
            raise StateConflict([f"새 {otype} 의 {m.property} '{values.get(m.property)}' 는 처음 상태가 아닙니다 "
                                 f"(initial: {', '.join(m.initial)})"])
        pk_name = ot.source.primary_key
        pk_col = ot.properties[pk_name].column or pk_name
        if ot.source.id_strategy == "uuid":
            values[pk_name] = str(uuid.uuid4())
        elif ot.source.id_strategy == "prefixed_sequence":
            values[pk_name] = self._next_id(ot.source.table, pk_col, ot.source.id_prefix)
        missing = [n for n, p in ot.properties.items() if p.required and n not in values]
        if missing:
            raise QueryError(f"{otype} 생성에 필수 속성 {missing} 의 값이 없습니다")
        cols = [ot.properties[k].column or k for k in values]
        self.conn.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(ot.source.table), sql.SQL(", ").join(map(sql.Identifier, cols)),
                sql.SQL(", ").join(sql.Placeholder() * len(values))),
            tuple(values.values()))
        if m:
            res.transitions.append({"object_type": otype, "pk": values[pk_name], "property": m.property,
                                    "from": None, "to": values[m.property]})
        return {"op": "create", "object_type": otype, "pk": values[pk_name], "after": values}

    def _next_id(self, table: str, pk_col: str, prefix: str) -> str:
        """T00507 → T00508. 동시에 두 Action 이 같은 번호를 받지 않도록 트랜잭션 잠금을 건다"""
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"minionto:{table}",))
        row = self.conn.execute(
            sql.SQL("SELECT max({c}) AS m FROM {t} WHERE {c} ~ %s").format(
                c=sql.Identifier(pk_col), t=sql.Identifier(table)),
            (f"^{prefix}[0-9]+$",)).fetchone()
        last = row["m"]
        width = len(last) - len(prefix) if last else 5
        n = int(last[len(prefix):]) + 1 if last else 1
        return f"{prefix}{n:0{width}d}"

    # ------------------------------------------------------------------ 6단계: 이벤트·감사
    def _record_transitions(self, res: ActionResult, actor: dict) -> None:
        for t in res.transitions:
            self.conn.execute(
                f"""INSERT INTO {ENGINE_SCHEMA}.state_transitions
                    (execution_id, action, object_type, target_pk, property, from_state, to_state, actor)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (res.execution_id, res.action, t["object_type"], str(t["pk"]), t["property"],
                 t["from"], t["to"], _json(actor)))

    def _emit(self, act: Action, res: ActionResult, target: Any, params: dict, actor: dict) -> list[str]:
        for event in act.emits:
            self._event(event, res.execution_id, act.target, target,
                        {"action": res.action, "actor": actor, "params": params, "changes": res.changes})
        return list(act.emits)

    def _log(self, res: ActionResult, actor: dict, params: dict) -> None:
        error = {"code": res.error_code, "retryable": res.retryable} if res.error_code else None
        self.conn.execute(
            f"""INSERT INTO {ENGINE_SCHEMA}.action_log
                (execution_id, action, object_type, target_pk, actor, params, status, messages, changes, approval_id,
                 error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (res.execution_id, res.action, res.object_type, str(res.target), _json(actor), _json(params),
             res.status, _json(res.messages), _json(res.changes), res.approval_id,
             _json(error) if error else None))
