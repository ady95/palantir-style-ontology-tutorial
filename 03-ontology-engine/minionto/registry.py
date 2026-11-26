"""Ontology Registry — 온톨로지 정의의 버전을 등록·비교·활성화한다 (19장)

    registry = Registry()                                   # .env 의 MINIONTO_REGISTRY_URL 또는 DATABASE_URL
    r = registry.register("ontologies/taxos/ontology.yaml", actor={"id": "A001", "type": "human"})
    for c in r.changes:                                    # 현재 활성 버전과 무엇이 달라졌나
        print(c)
    registry.activate("taxos", r.version.version, actor={"id": "A005", "type": "human"})
    client = registry.get("taxos").client(today="2027-01-18")   # 활성 버전으로 엔진 열기

원칙
- 등록된 버전은 바꾸지 않는다. 내용이 바뀌면 version 을 올려 새로 등록한다 (checksum 으로 확인).
- 등록(draft)과 운영 반영(active)을 나눈다. 활성화는 사람이, 등록자가 아닌 다른 사람이 한다.
  LLM 이 만든 정의 초안도 이 관문을 지나야 실행 환경에 들어간다.
- 활성화 전에 DB 대조 검사를 다시 하고, 기존 사용자를 깨는 변경(breaking)은 명시적으로 허용해야 한다.
- 정의(YAML)와 Function 구현 코드를 함께 저장한다. 활성 버전은 파일이 바뀌어도 그대로 재현된다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .loader import OntologyLoadError, load
from .metamodel import Ontology, Parameter
from .validator import validate

REGISTRY_SCHEMA = "minionto_registry"
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {REGISTRY_SCHEMA};
CREATE TABLE IF NOT EXISTS {REGISTRY_SCHEMA}.ontology_versions (
    name             text        NOT NULL,
    version          text        NOT NULL,
    status           text        NOT NULL CHECK (status IN ('draft', 'active', 'retired')),
    checksum         text        NOT NULL,
    source_yaml      text        NOT NULL,
    definition       jsonb       NOT NULL,
    function_sources jsonb       NOT NULL DEFAULT '{{}}',
    registered_by    jsonb       NOT NULL,
    registered_at    timestamptz NOT NULL DEFAULT now(),
    activated_by     jsonb,
    activated_at     timestamptz,
    notes            text,
    PRIMARY KEY (name, version)
);
-- 온톨로지마다 활성 버전은 하나뿐
CREATE UNIQUE INDEX IF NOT EXISTS ontology_versions_one_active
    ON {REGISTRY_SCHEMA}.ontology_versions (name) WHERE status = 'active';
"""


class RegistryError(Exception):
    def __init__(self, message: str, details: list[str] | None = None):
        self.details = details or []
        super().__init__(message + "".join(f"\n  - {d}" for d in self.details))


# ====================================================================== 변경 비교
LEVELS = ("breaking", "policy", "compatible")


@dataclass
class Change:
    level: str      # breaking: 기존 API·SDK 사용자가 깨짐 | policy: 업무 규칙이 바뀜(검토 필요) | compatible
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.path}: {self.message}"


def _dump(model) -> Any:
    return model.model_dump(by_alias=True, exclude_none=True) if model is not None else None


def _params(changes: list[Change], path: str, old: dict[str, Parameter], new: dict[str, Parameter]) -> None:
    for name in old.keys() - new.keys():
        changes.append(Change("breaking", f"{path}.{name}", "입력 삭제"))
    for name in new.keys() - old.keys():
        p = new[name]
        if p.required and p.default is None:
            changes.append(Change("breaking", f"{path}.{name}", "필수 입력 추가 (기존 호출이 실패함)"))
        else:
            changes.append(Change("compatible", f"{path}.{name}", "선택 입력 추가"))
    for name in old.keys() & new.keys():
        o, n = old[name], new[name]
        if (o.type, o.object_type, o.items) != (n.type, n.object_type, n.items):
            changes.append(Change("breaking", f"{path}.{name}", f"타입 변경 {o.type} → {n.type}"))
        elif not o.required and n.required and n.default is None:
            changes.append(Change("breaking", f"{path}.{name}", "선택 → 필수"))
        if o.values and n.values and set(o.values) - set(n.values):
            changes.append(Change("breaking", f"{path}.{name}",
                                  f"허용 값 삭제: {', '.join(sorted(set(o.values) - set(n.values)))}"))


def diff(old: Ontology, new: Ontology) -> list[Change]:
    """두 정의의 차이를 '사용자에게 미치는 영향' 기준으로 분류한다"""
    ch: list[Change] = []

    # ---- Object Type · Property
    for name in old.object_types.keys() - new.object_types.keys():
        ch.append(Change("breaking", f"object_types.{name}", "객체 타입 삭제"))
    for name in new.object_types.keys() - old.object_types.keys():
        ch.append(Change("compatible", f"object_types.{name}", "객체 타입 추가"))
    for name in sorted(old.object_types.keys() & new.object_types.keys()):
        o, n, p = old.object_types[name], new.object_types[name], f"object_types.{name}"
        if o.source.primary_key != n.source.primary_key:
            ch.append(Change("breaking", f"{p}.source.primary_key", "기본키 변경"))
        if o.source.table != n.source.table:
            ch.append(Change("compatible", f"{p}.source.table",
                             f"물리 테이블 변경 {o.source.table} → {n.source.table} (API 영향 없음)"))
        for prop in sorted(o.properties.keys() - n.properties.keys()):
            ch.append(Change("breaking", f"{p}.properties.{prop}", "속성 삭제"))
        for prop in sorted(n.properties.keys() - o.properties.keys()):
            ch.append(Change("compatible", f"{p}.properties.{prop}", "속성 추가"))
        for prop in sorted(o.properties.keys() & n.properties.keys()):
            op, np_, pp = o.properties[prop], n.properties[prop], f"{p}.properties.{prop}"
            if op.type != np_.type:
                ch.append(Change("breaking", pp, f"타입 변경 {op.type} → {np_.type}"))
            if (op.column or prop) != (np_.column or prop):
                ch.append(Change("compatible", pp, f"컬럼 매핑 변경 → {np_.column or prop} (API 영향 없음)"))
            removed = set(op.values or []) - set(np_.values or [])
            added = set(np_.values or []) - set(op.values or [])
            if removed:
                ch.append(Change("breaking", pp, f"enum 값 삭제: {', '.join(sorted(removed))}"))
            if added:
                ch.append(Change("compatible", pp, f"enum 값 추가: {', '.join(sorted(added))}"))

    # ---- Link Type: 사용자는 forward_name·reverse_name 으로 링크를 부른다
    for name in old.link_types.keys() - new.link_types.keys():
        lt = old.link_types[name]
        ch.append(Change("breaking", f"link_types.{name}",
                         f"링크 삭제 ({lt.from_}.{lt.forward_name}, {lt.to}.{lt.reverse_name})"))
    for name in new.link_types.keys() - old.link_types.keys():
        ch.append(Change("compatible", f"link_types.{name}", "링크 추가"))
    for name in sorted(old.link_types.keys() & new.link_types.keys()):
        o, n, p = old.link_types[name], new.link_types[name], f"link_types.{name}"
        if (o.from_, o.to, o.forward_name, o.reverse_name) != (n.from_, n.to, n.forward_name, n.reverse_name):
            ch.append(Change("breaking", p, "링크 이름 또는 연결 대상 변경"))
        if o.cardinality != n.cardinality:
            ch.append(Change("breaking", p, f"카디널리티 변경 {o.cardinality} → {n.cardinality}"))
        if (o.from_key, o.to_key, _dump(o.through)) != (n.from_key, n.to_key, _dump(n.through)):
            ch.append(Change("compatible", p, "조인 키 변경 (API 영향 없음)"))

    # ---- Function
    for name in old.functions.keys() - new.functions.keys():
        ch.append(Change("breaking", f"functions.{name}", "Function 삭제"))
    for name in new.functions.keys() - old.functions.keys():
        ch.append(Change("compatible", f"functions.{name}", "Function 추가"))
    for name in sorted(old.functions.keys() & new.functions.keys()):
        o, n, p = old.functions[name], new.functions[name], f"functions.{name}"
        _params(ch, f"{p}.inputs", o.inputs, n.inputs)
        if _dump(o.returns) != _dump(n.returns):
            ch.append(Change("breaking", f"{p}.returns", "반환 형식 변경"))
        if o.implementation != n.implementation:
            ch.append(Change("policy", f"{p}.implementation", f"구현 위치 변경 → {n.implementation}"))

    # ---- Action: 입력 형식은 호환성, 규칙(조건·권한·승인·변경 내용)은 정책 검토 대상
    for name in old.actions.keys() - new.actions.keys():
        ch.append(Change("breaking", f"actions.{name}", "Action 삭제"))
    for name in new.actions.keys() - old.actions.keys():
        ch.append(Change("compatible", f"actions.{name}", "Action 추가"))
    for name in sorted(old.actions.keys() & new.actions.keys()):
        o, n, p = old.actions[name], new.actions[name], f"actions.{name}"
        if o.target != n.target:
            ch.append(Change("breaking", f"{p}.target", f"대상 객체 변경 {o.target} → {n.target}"))
        _params(ch, f"{p}.parameters", o.parameters, n.parameters)
        for key, label in (("preconditions", "실행 조건"), ("permission", "권한"),
                           ("approval", "승인 규칙"), ("edits", "변경 내용")):
            ov, nv = getattr(o, key), getattr(n, key)
            ov = [_dump(x) for x in ov] if isinstance(ov, list) else _dump(ov)
            nv = [_dump(x) for x in nv] if isinstance(nv, list) else _dump(nv)
            if ov != nv:
                ch.append(Change("policy", f"{p}.{key}", f"{label} 변경"))
        if set(o.emits) - set(n.emits):
            ch.append(Change("breaking", f"{p}.emits",
                             f"이벤트 발행 중단: {', '.join(sorted(set(o.emits) - set(n.emits)))} (구독자 영향)"))
        if set(n.emits) - set(o.emits):
            ch.append(Change("compatible", f"{p}.emits", f"이벤트 추가: {', '.join(sorted(set(n.emits) - set(o.emits)))}"))

    for role in sorted(set(old.roles) - set(new.roles)):
        ch.append(Change("policy", "roles", f"역할 삭제: {role}"))
    return sorted(ch, key=lambda c: (LEVELS.index(c.level), c.path))


def suggest_version(current: str, changes: list[Change]) -> str:
    """breaking → 주 버전, 그 밖의 변경 → 부 버전, 변경 없음(코드만 수정 등) → 수 버전"""
    try:
        major, minor, patch = (int(x) for x in current.split(".")[:3])
    except ValueError:
        return current
    if any(c.level == "breaking" for c in changes):
        return f"{major + 1}.0.0"
    if changes:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# ====================================================================== 패키징
@dataclass
class Package:
    """등록 단위: 정의 + Function 구현 코드 + checksum"""
    onto: Ontology
    source_yaml: str
    function_sources: dict[str, str]
    checksum: str

    @classmethod
    def from_file(cls, path: str | Path) -> Package:
        path = Path(path)
        onto = load(path)
        report = validate(onto, base_dir=path.resolve().parent)
        if not report.ok:
            raise RegistryError(f"검증 실패 — {path}", [str(i) for i in report.errors])
        sources = {}
        for fn in onto.functions.values():
            module = fn.implementation.split(":")[0]
            file = path.resolve().parent / (module.replace(".", "/") + ".py")
            if not file.exists():
                raise RegistryError(f"Function 구현 파일이 없습니다: {file.name}")
            sources[module] = file.read_text(encoding="utf-8")
        return cls.from_text(path.read_text(encoding="utf-8"), sources)

    @classmethod
    def from_text(cls, source_yaml: str, function_sources: dict[str, str] | None = None) -> Package:
        try:
            onto = Ontology.model_validate(yaml.safe_load(source_yaml))
        except ValidationError as e:
            raise RegistryError("정의 문법 오류", [f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                               for x in e.errors()]) from None
        sources = dict(sorted((function_sources or {}).items()))
        h = hashlib.sha256(source_yaml.encode())
        for module, code in sources.items():
            h.update(f"\0{module}\0{code}".encode())
        return cls(onto, source_yaml, sources, h.hexdigest())


@dataclass
class OntologyVersion:
    name: str
    version: str
    status: str
    checksum: str
    onto: Ontology
    function_sources: dict[str, str]
    registered_by: dict
    registered_at: datetime
    activated_by: dict | None = None
    activated_at: datetime | None = None
    notes: str | None = None

    def client(self, url: str | None = None, **kw):
        """이 버전의 정의·Function 코드로 엔진을 연다 (구현 파일을 디스크에서 읽지 않는다)"""
        from .objects import OntologyClient
        return OntologyClient(self.onto, url, function_sources=self.function_sources, **kw)

    def __str__(self) -> str:
        return f"{self.name} v{self.version} [{self.status}] {self.checksum[:12]}"


@dataclass
class RegisterResult:
    version: OntologyVersion
    created: bool                       # False 면 같은 내용이 이미 등록되어 있었음
    base: str | None                    # 비교 기준(현재 활성) 버전
    changes: list[Change] = field(default_factory=list)
    suggested_version: str | None = None


# ====================================================================== Registry
class Registry:
    def __init__(self, url: str | None = None, *, conn: psycopg.Connection | None = None):
        self._url = url
        self._conn = conn
        self._ready = False

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            from .db import REPO_ROOT
            load_dotenv(REPO_ROOT / ".env")
            url = self._url or os.getenv("MINIONTO_REGISTRY_URL") or os.getenv("DATABASE_URL")
            if not url:
                raise RegistryError("Registry DB 접속 정보가 없습니다 (MINIONTO_REGISTRY_URL 또는 DATABASE_URL)")
            self._conn = psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"),
                                         autocommit=True, row_factory=dict_row, connect_timeout=5)
        if not self._ready:
            self._conn.execute(DDL)
            self._ready = True
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 조회
    def _row(self, name: str, version: str | None, *, lock: bool = False) -> dict | None:
        q = f"SELECT * FROM {REGISTRY_SCHEMA}.ontology_versions WHERE name = %s"
        q += " AND version = %s" if version else " AND status = 'active'"
        return self.conn.execute(q + (" FOR UPDATE" if lock else ""),
                                 (name, version) if version else (name,)).fetchone()

    @staticmethod
    def _version(row: dict) -> OntologyVersion:
        pkg = Package.from_text(row["source_yaml"], row["function_sources"])
        return OntologyVersion(row["name"], row["version"], row["status"], row["checksum"], pkg.onto,
                               pkg.function_sources, row["registered_by"], row["registered_at"],
                               row["activated_by"], row["activated_at"], row["notes"])

    def get(self, name: str, version: str | None = None) -> OntologyVersion:
        """version 을 생략하면 활성 버전"""
        row = self._row(name, version)
        if row is None:
            raise RegistryError(f"'{name}' " + (f"v{version} 이 등록되어 있지 않습니다" if version
                                                else "의 활성 버전이 없습니다"))
        return self._version(row)

    def active(self) -> dict[str, OntologyVersion]:
        rows = self.conn.execute(
            f"SELECT * FROM {REGISTRY_SCHEMA}.ontology_versions WHERE status = 'active' ORDER BY name").fetchall()
        return {r["name"]: self._version(r) for r in rows}

    def history(self, name: str | None = None) -> list[dict]:
        q = f"""SELECT name, version, status, left(checksum, 12) AS checksum,
                       registered_by->>'id' AS registered_by, registered_at,
                       activated_by->>'id' AS activated_by, activated_at, notes
                FROM {REGISTRY_SCHEMA}.ontology_versions"""
        if name:
            return self.conn.execute(q + " WHERE name = %s ORDER BY registered_at", (name,)).fetchall()
        return self.conn.execute(q + " ORDER BY name, registered_at").fetchall()

    def diff(self, name: str, old: str, new: str) -> list[Change]:
        return diff(self.get(name, old).onto, self.get(name, new).onto)

    # ------------------------------------------------------------------ 등록
    def register(self, source: str | Path | Package, *, actor: dict, notes: str | None = None) -> RegisterResult:
        """검증을 통과한 정의를 draft 로 등록한다. 같은 버전·같은 내용이면 기존 것을 돌려준다."""
        if not actor.get("id"):
            raise RegistryError("등록자(actor.id)가 필요합니다")
        try:
            pkg = source if isinstance(source, Package) else Package.from_file(source)
        except OntologyLoadError as e:
            raise RegistryError("정의 문법 오류", e.problems) from None
        name, version = pkg.onto.ontology.name, pkg.onto.ontology.version
        with self.conn.transaction():
            existing = self._row(name, version, lock=True)
            if existing is not None:
                if existing["checksum"] != pkg.checksum:
                    raise RegistryError(f"{name} v{version} 은 이미 다른 내용으로 등록되어 있습니다. "
                                        "등록된 버전은 바꿀 수 없으니 version 을 올리세요")
                created = False
            else:
                self.conn.execute(
                    f"""INSERT INTO {REGISTRY_SCHEMA}.ontology_versions
                        (name, version, status, checksum, source_yaml, definition, function_sources,
                         registered_by, notes)
                        VALUES (%s, %s, 'draft', %s, %s, %s, %s, %s, %s)""",
                    (name, version, pkg.checksum, pkg.source_yaml,
                     Jsonb(json.loads(json.dumps(yaml.safe_load(pkg.source_yaml), default=str))),
                     Jsonb(pkg.function_sources), Jsonb(actor), notes))
                created = True
            stored = self._version(self._row(name, version))
        base = self._row(name, None)
        result = RegisterResult(stored, created, base["version"] if base else None)
        if base and base["version"] != version:
            base_v = self._version(base)
            result.changes = diff(base_v.onto, pkg.onto)
            if base_v.checksum != pkg.checksum and not result.changes and \
                    base_v.function_sources != pkg.function_sources:
                result.changes = [Change("policy", "functions", "Function 구현 코드 변경")]
            result.suggested_version = suggest_version(base["version"], result.changes)
        return result

    # ------------------------------------------------------------------ 활성화
    def activate(self, name: str, version: str, *, actor: dict, allow_breaking: bool = False,
                 check_db: bool = True, db_url: str | None = None) -> list[Change]:
        """draft(또는 retired) 버전을 운영에 반영한다. 기존 활성 버전은 retired 가 된다.

        retired 버전을 다시 활성화하면 그것이 곧 롤백이다.
        """
        if actor.get("type") != "human":
            raise RegistryError("정의 활성화는 사람만 할 수 있습니다 (AI 가 만든 정의도 사람이 검토 후 반영)")
        with self.conn.transaction():
            row = self._row(name, version, lock=True)
            if row is None:
                raise RegistryError(f"'{name}' v{version} 이 등록되어 있지 않습니다")
            if row["status"] == "active":
                raise RegistryError(f"'{name}' v{version} 은 이미 활성 버전입니다")
            if row["registered_by"].get("id") == actor.get("id") and row["status"] == "draft":
                raise RegistryError("등록자 본인은 활성화할 수 없습니다 (직무 분리: 다른 사람이 검토)")
            target = self._version(row)
            current = self._row(name, None, lock=True)
            changes = diff(self._version(current).onto, target.onto) if current else []
            breaking = [c for c in changes if c.level == "breaking"]
            if breaking and not allow_breaking:
                raise RegistryError(f"v{current['version']} → v{version}: 기존 사용자를 깨는 변경이 "
                                    f"{len(breaking)}건 있습니다 (allow_breaking 으로 허용)",
                                    [str(c) for c in breaking])
            if check_db:                                 # 정의 비교(DB 불필요) 다음에 실제 스키마 대조
                from .db import check_database, database_url
                report = check_database(target.onto, database_url(target.onto, db_url))
                if not report.ok:
                    raise RegistryError("DB 대조 검사 실패 — 정의가 실제 스키마와 맞지 않습니다",
                                        [str(i) for i in report.errors])
            if current:
                self.conn.execute(f"UPDATE {REGISTRY_SCHEMA}.ontology_versions SET status = 'retired' "
                                  "WHERE name = %s AND version = %s", (name, current["version"]))
            self.conn.execute(
                f"""UPDATE {REGISTRY_SCHEMA}.ontology_versions
                    SET status = 'active', activated_by = %s, activated_at = now()
                    WHERE name = %s AND version = %s""", (Jsonb(actor), name, version))
        return changes
