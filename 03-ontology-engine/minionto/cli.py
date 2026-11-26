"""MiniOnto 명령줄 도구

  python -m minionto validate ontologies/taxos/ontology.yaml [--db] [--db-url URL]
  python -m minionto describe ontologies/taxos/ontology.yaml [--db]
  python -m minionto query ontologies/taxos/ontology.yaml TaxReturn -w status=자료수집중 -p customer [--sql]
  python -m minionto schema [-o schema/minionto.schema.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .loader import OntologyLoadError, load
from .metamodel import Ontology
from .schema import Schema
from .validator import validate


def _load_or_exit(path: str) -> Ontology:
    try:
        return load(path)
    except OntologyLoadError as e:
        print(e)
        sys.exit(1)


def cmd_validate(args) -> int:
    onto = _load_or_exit(args.file)
    print(f"[1/3] 문법 검사 OK  — {onto.ontology.name} v{onto.ontology.version}")
    report = validate(onto, base_dir=Path(args.file).resolve().parent)
    print(f"[2/3] 참조 검사     — 오류 {len(report.errors)}, 경고 {len(report.warnings)}")
    if args.db or args.db_url:
        from .db import check_database, database_url
        before = len(report.issues)
        check_database(onto, database_url(onto, args.db_url), report)
        added = report.issues[before:]
        print(f"[3/3] DB 스키마 대조 — 오류 {sum(i.level == 'error' for i in added)}, "
              f"경고 {sum(i.level == 'warning' for i in added)}")
    else:
        print("[3/3] DB 스키마 대조 — 건너뜀 (--db)")
    for issue in report.issues:
        print("  " + str(issue))
    print("결과:", "통과" if report.ok else "실패")
    return 0 if report.ok else 1


def cmd_describe(args) -> int:
    onto = _load_or_exit(args.file)
    schema = Schema(onto)
    counts = {}
    if args.db or args.db_url:
        from .db import count_rows, database_url
        counts = count_rows(onto, database_url(onto, args.db_url))

    info = onto.ontology
    print(f"# {info.display_name or info.name}  (v{info.version})")
    print(f"Object Type {len(onto.object_types)} · Link Type {len(onto.link_types)} · "
          f"Function {len(onto.functions)} · Action {len(onto.actions)} · Interface {len(onto.interfaces)}")
    for name, ot in onto.object_types.items():
        n = f"  [{counts[name]:,}행]" if name in counts else ""
        impl = f"  implements {', '.join(ot.implements)}" if ot.implements else ""
        print(f"\n## {name} ({ot.display_name}) ← {ot.source.table}{n}{impl}")
        print(f"   속성 {len(ot.properties)}개: " + ", ".join(ot.properties))
        for end in schema.links[name].values():
            arrow = "→ 여러" if end.many else "→ 하나"
            print(f"   .{end.name:18s} {arrow} {end.target}")
        acts = [a for a, act in onto.actions.items() if act.target == name]
        if acts:
            print(f"   Action: {', '.join(acts)}")
    if onto.functions:
        print("\n## Functions")
        for name, fn in onto.functions.items():
            ins = ", ".join(f"{k}: {v.object_type or v.type}" for k, v in fn.inputs.items())
            print(f"   {name}({ins}) -> {fn.returns.object_type or fn.returns.type}")
    return 0


_WHERE = re.compile(r"^\s*([\w.$]+)\s*(!=|>=|<=|=|>|<)\s*(.*?)\s*$")
_CLI_OPS = {"=": "eq", "!=": "ne", ">=": "gte", "<=": "lte", ">": "gt", "<": "lt"}


def parse_where(text: str) -> dict:
    """'customer.risk_level=높음', 'status=미요청|요청됨'(in), 'filed_at=null' → 조건식"""
    m = _WHERE.match(text)
    if not m:
        raise SystemExit(f"조건 형식 오류: '{text}' (예: status=자료수집중, due_date<=2027-01-25)")
    path, op, value = m.groups()
    if value.lower() == "null" and op in ("=", "!="):
        return {"field": path, "op": "is_null" if op == "=" else "not_null"}
    if "|" in value and op in ("=", "!="):
        return {"field": path, "op": "in" if op == "=" else "not_in", "value": value.split("|")}
    return {"field": path, "op": _CLI_OPS[op], "value": value}


class _Step(argparse.Action):
    """-w 와 -p 를 입력한 순서대로 쌓는다 (조건 → pivot → 조건 ...)"""
    def __call__(self, parser, ns, value, option_string=None):
        steps = getattr(ns, "steps", None) or []
        steps.append((self.const, value))
        ns.steps = steps


def cmd_query(args) -> int:
    from .objects import OntologyClient
    from .query import QueryError
    onto = _load_or_exit(args.file)
    client = OntologyClient(onto, args.db_url, today=args.today)
    try:
        q = client.objects(args.type)
        for kind, value in args.steps or []:
            q = q.where(parse_where(value)) if kind == "where" else q.pivot(value)
        if args.order:
            q = q.order_by(*args.order.split(","))
        q = q.limit(args.limit)
        if args.sql:
            print(q.sql() if not args.count_by else "(count-by 는 실행 시 SQL을 출력합니다)")
            return 0
        if args.count_by:
            rows = q.count_by(*args.count_by.split(","))
        elif args.select:
            rows = q.rows(*args.select.split(","))
        else:
            rows = [o.properties for o in q.all()]
        total = q.count()
    except QueryError as e:
        print(f"질의 오류: {e}")
        return 1
    finally:
        client.close()
    print(f"# {q.object_type}  — 전체 {total:,}건" + (f", 표시 {len(rows)}건" if not args.count_by else ""))
    if rows:
        keys = list(rows[0])
        print(" | ".join(keys))
        for r in rows:
            print(" | ".join("" if r[k] is None else str(r[k]) for k in keys))
    if args.verbose:
        print("\n-- 실행한 SQL")
        for s in client.query_log:
            print(s + ";\n")
    return 0


def cmd_schema(args) -> int:
    text = json.dumps(Ontology.model_json_schema(by_alias=True), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"JSON Schema → {args.output}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="minionto", description="MiniOnto 온톨로지 정의 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("validate", cmd_validate, "정의 검증"), ("describe", cmd_describe, "정의 요약")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("file")
        sp.add_argument("--db", action="store_true", help=".env 의 접속 정보로 DB 대조")
        sp.add_argument("--db-url", help="DB 접속 URL 직접 지정")
        sp.set_defaults(func=fn)
    sp = sub.add_parser("query", help="객체 조회·링크 탐색 (Object/Link Engine)")
    sp.add_argument("file")
    sp.add_argument("type", help="시작 객체 타입 (예: TaxReturn)")
    sp.add_argument("-w", "--where", action=_Step, const="where", dest="steps", metavar="COND",
                    help="조건 (반복 가능): status=자료수집중, customer.risk_level=높음, status=미요청|요청됨")
    sp.add_argument("-p", "--pivot", action=_Step, const="pivot", dest="steps", metavar="LINK",
                    help="링크로 이어진 객체 집합으로 이동 (반복 가능)")
    sp.add_argument("--order", help="정렬 경로 (쉼표 구분, 내림차순은 -경로)")
    sp.add_argument("--select", help="표시할 경로 (쉼표 구분, 링크 경로 가능)")
    sp.add_argument("--count-by", help="그룹별 개수 (쉼표 구분 경로)")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--today", help="$today 기준일 (예: 2027-01-18)")
    sp.add_argument("--sql", action="store_true", help="실행하지 않고 SQL만 출력")
    sp.add_argument("-v", "--verbose", action="store_true", help="실행한 SQL 출력")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("schema", help="에디터 자동완성용 JSON Schema 출력")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_schema)
    args = ap.parse_args(argv)
    return args.func(args)
