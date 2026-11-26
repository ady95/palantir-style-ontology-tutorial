"""온톨로지 정의와 실제 PostgreSQL 스키마 대조

온톨로지는 운영 DB 위에 얹는 '의미 계층'이다. 정의가 가리키는 테이블·컬럼이
실제로 있고 타입이 맞는지 확인해야 엔진이 안전하게 질의할 수 있다.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql

from .metamodel import Ontology
from .validator import Report

# PostgreSQL data_type → 허용되는 DSL 속성 타입
PG_TYPES = {
    "character varying": {"string", "enum"},
    "character": {"string", "enum"},
    "text": {"string", "enum"},
    "smallint": {"integer"},
    "integer": {"integer"},
    "bigint": {"integer"},
    "real": {"float", "decimal"},
    "double precision": {"float", "decimal"},
    "numeric": {"decimal", "float", "integer"},
    "boolean": {"boolean"},
    "date": {"date"},
    "timestamp without time zone": {"datetime"},
    "timestamp with time zone": {"datetime"},
    "bytea": {"binary"},
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def database_url(onto: Ontology, override: str | None = None) -> str:
    """환경변수(또는 저장소 루트 .env)에서 접속 URL을 읽는다. SQLAlchemy 형식도 허용."""
    if override:
        url = override
    else:
        load_dotenv(REPO_ROOT / ".env")
        url = os.getenv(onto.datasource.url_env)
        if not url:
            raise RuntimeError(f"환경변수 {onto.datasource.url_env} 가 없습니다 (.env 확인)")
    return url.replace("postgresql+psycopg://", "postgresql://")


def fetch_columns(conn: psycopg.Connection) -> dict[str, dict[str, str]]:
    rows = conn.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
    """).fetchall()
    tables: dict[str, dict[str, str]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, {})[column] = dtype
    return tables


def fetch_primary_keys(conn: psycopg.Connection) -> dict[str, set[str]]:
    rows = conn.execute("""
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
    """).fetchall()
    pks: dict[str, set[str]] = {}
    for table, column in rows:
        pks.setdefault(table, set()).add(column)
    return pks


def check_database(onto: Ontology, url: str, report: Report | None = None) -> Report:
    report = report or Report()
    with psycopg.connect(url, connect_timeout=5) as conn:
        tables = fetch_columns(conn)
        pks = fetch_primary_keys(conn)

    for name, ot in onto.object_types.items():
        p = f"object_types.{name}"
        table = ot.source.table
        if table not in tables:
            report.error(f"{p}.source.table", f"테이블 '{table}' 이 DB에 없습니다")
            continue
        cols = tables[table]
        for pname, prop in ot.properties.items():
            col = prop.column or pname
            if col not in cols:
                report.error(f"{p}.properties.{pname}", f"컬럼 '{table}.{col}' 이 없습니다")
                continue
            allowed = PG_TYPES.get(cols[col])
            if allowed is None:
                report.warning(f"{p}.properties.{pname}", f"알 수 없는 DB 타입 '{cols[col]}'")
            elif prop.type not in allowed:
                report.error(f"{p}.properties.{pname}",
                             f"타입 불일치: DSL {prop.type} ↔ DB {cols[col]} ({table}.{col})")
        pk_prop = ot.properties.get(ot.source.primary_key)
        if pk_prop:
            pk_col = pk_prop.column or ot.source.primary_key
            if pk_col not in pks.get(table, set()):
                report.warning(f"{p}.source.primary_key",
                               f"'{table}.{pk_col}' 에 PRIMARY KEY 제약이 없습니다 (유일성을 DB가 보장하지 않음)")
        unmapped = set(cols) - {prop.column or n for n, prop in ot.properties.items()}
        if unmapped:
            report.warning(p, f"온톨로지에 매핑하지 않은 컬럼: {', '.join(sorted(unmapped))}")

    for name, lt in onto.link_types.items():
        if lt.through is None:
            continue
        t = lt.through
        p = f"link_types.{name}.through"
        if t.table not in tables:
            report.error(f"{p}.table", f"테이블 '{t.table}' 이 DB에 없습니다")
            continue
        for col in (t.from_column, t.to_column):
            if col not in tables[t.table]:
                report.error(p, f"컬럼 '{t.table}.{col}' 이 없습니다")
    return report


def count_rows(onto: Ontology, url: str) -> dict[str, int]:
    """객체 타입별 행 수 (describe 명령용)"""
    counts = {}
    with psycopg.connect(url, connect_timeout=5) as conn:
        for name, ot in onto.object_types.items():
            q = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(ot.source.table))
            counts[name] = conn.execute(q).fetchone()[0]
    return counts
