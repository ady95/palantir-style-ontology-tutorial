"""MiniOnto 명령줄 도구

  python -m minionto validate ontologies/taxos/ontology.yaml [--db] [--db-url URL]
  python -m minionto describe ontologies/taxos/ontology.yaml [--db]
  python -m minionto query ontologies/taxos/ontology.yaml TaxReturn -w status=자료수집중 -p customer [--sql]
  python -m minionto call ontologies/taxos/ontology.yaml filing_delay_risk -a customer=C0008
  python -m minionto run ontologies/taxos/ontology.yaml request_missing_documents R00006 --actor agent-01,AI비서,agent
  python -m minionto states ontologies/taxos/ontology.yaml TaxReturn [--mermaid]
  python -m minionto available ontologies/taxos/ontology.yaml TaxReturn R00006 --actor agent-01,AI비서,agent
  python -m minionto history ontologies/taxos/ontology.yaml TaxReturn R00006
  python -m minionto token issue --sub agent-01 --role AI비서 --type agent --scope objects:read,actions:apply --ttl 1h
  python -m minionto serve ontologies/taxos/ontology.yaml --auth jwt
  python -m minionto query ontologies/taxos/ontology.yaml Customer --as A003,실무자,human
  python -m minionto approvals list|expire ontologies/taxos/ontology.yaml
  python -m minionto decisions ontologies/taxos/ontology.yaml
  python -m minionto registry register ontologies/taxos/ontology.yaml --actor A001,human
  python -m minionto registry activate taxos 0.1.0 --actor A005,human
  python -m minionto serve ontologies/taxos/ontology.yaml --keys examples/dev_api_keys.yaml   (또는 --registry)
  python -m minionto sdk ontologies/taxos/ontology.yaml -o build/taxos_sdk.py
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
        if args.as_:                                     # 21장: 이 실행자에게 보이는 범위로
            q = q.secure(_actor(args.as_))
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


def _kv(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        k, sep, v = item.partition("=")
        if not sep:
            raise SystemExit(f"'키=값' 형식이 아닙니다: {item}")
        out[k] = v
    return out


def cmd_call(args) -> int:
    from .functions import FunctionError
    from .objects import OntologyClient
    with OntologyClient.open(args.file, args.db_url, today=args.today) as client:
        try:
            result = client.functions.call(args.function, **_kv(args.arg))
        except FunctionError as e:
            print(f"Function 오류: {e}")
            return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str)
          if isinstance(result, (dict, list)) and not any(hasattr(x, "pk") for x in result) else result)
    return 0


def _actor(text: str) -> dict:
    try:
        aid, role, atype = text.split(",")
    except ValueError:
        raise SystemExit("--actor 는 '아이디,역할,유형' 형식입니다 (예: A001,세무사,human)") from None
    return {"id": aid, "role": role, "type": atype}


def cmd_run(args) -> int:
    from .objects import OntologyClient
    with OntologyClient.open(args.file, args.db_url, today=args.today) as client:
        result = client.actions.apply(args.action, args.target, actor=_actor(args.actor),
                                      params=_kv(args.arg), dry_run=not args.commit,
                                      expect=_kv(args.expect) or None,
                                      idempotency_key=args.idempotency_key, retries=args.retries)
    print(result)
    if result.replayed:
        print("(멱등 키: 실행하지 않고 처음 결과를 돌려줌)")
    if result.attempts > 1:
        print(f"(시도 {result.attempts}회)")
    if not args.commit and result.ok:
        print("(dry-run: 반영하려면 --commit)")
    return 0 if result.status in ("completed", "approval_required") else 1


def _registry_actor(text: str) -> dict:
    parts = text.split(",")
    if len(parts) == 2:
        return {"id": parts[0], "type": parts[1]}
    if len(parts) == 3:
        return {"id": parts[0], "role": parts[1], "type": parts[2]}
    raise SystemExit("--actor 는 '아이디,유형' 또는 '아이디,역할,유형' 형식입니다 (예: A001,human)")


def _table(rows: list[dict]) -> None:
    if not rows:
        print("(없음)")
        return
    keys = list(rows[0])
    print(" | ".join(keys))
    for r in rows:
        print(" | ".join("" if r[k] is None else str(r[k])[:19] if hasattr(r[k], "isoformat") else str(r[k])
                         for k in keys))


def cmd_registry(args) -> int:
    from .registry import Registry, RegistryError
    with Registry(args.registry_url) as reg:
        try:
            if args.op == "register":
                r = reg.register(args.file, actor=_registry_actor(args.actor), notes=args.notes)
                print(("등록" if r.created else "이미 등록됨(같은 내용)") + f": {r.version}")
                if r.base and r.base != r.version.version:
                    print(f"현재 활성 v{r.base} 대비 변경 {len(r.changes)}건 — 권장 버전 v{r.suggested_version}")
                    for c in r.changes:
                        print("  " + str(c))
                elif not r.base:
                    print("활성 버전이 없습니다. activate 로 반영하세요")
            elif args.op == "activate":
                changes = reg.activate(args.name, args.version, actor=_registry_actor(args.actor),
                                       allow_breaking=args.allow_breaking, check_db=not args.no_db_check)
                print(f"활성화: {args.name} v{args.version} (이전 활성 대비 변경 {len(changes)}건)")
            elif args.op == "diff":
                changes = reg.diff(args.name, args.old, args.new)
                print(f"{args.name} v{args.old} → v{args.new}: 변경 {len(changes)}건")
                for c in changes:
                    print("  " + str(c))
            else:
                _table(reg.history(args.name))
        except RegistryError as e:
            print(f"Registry 오류: {e}")
            return 1
    return 0


def _load_env() -> None:
    from dotenv import load_dotenv

    from .db import REPO_ROOT
    load_dotenv(REPO_ROOT / ".env")


def _ttl(text: str):
    from datetime import timedelta
    unit = {"m": "minutes", "h": "hours", "d": "days"}.get(text[-1:])
    if not unit or not text[:-1].isdigit():
        raise SystemExit("--ttl 은 30m · 1h · 7d 형식입니다")
    return timedelta(**{unit: int(text[:-1])})


def cmd_token(args) -> int:
    from .auth import AuthError, JwtAuth
    _load_env()
    try:
        auth = JwtAuth()
        token = auth.issue(sub=args.sub, role=args.role, actor_type=args.type,
                           scopes=[x for x in args.scope.split(",") if x], ttl=_ttl(args.ttl))
        if args.show:
            p = auth.authenticate(token)
            print(f"# {p.actor['id']} ({p.actor['role']}, {p.actor['type']}) · 범위 {', '.join(sorted(p.scopes))} "
                  f"· 만료 {p.expires_at:%Y-%m-%d %H:%M} UTC · jti {p.token_id[:8]}", file=sys.stderr)
    except AuthError as e:
        print(f"토큰 오류: {e}", file=sys.stderr)
        return 1
    print(token)
    return 0


def cmd_approvals(args) -> int:
    from .objects import OntologyClient
    with OntologyClient.open(args.file, args.db_url, today=args.today) as client:
        if args.op == "expire":
            closed = client.actions.expire_stale()
            print(f"기한이 지나 만료한 승인 요청: {len(closed)}건")
            return 0
        rows = client.actions.pending_approvals()
    if not rows:
        print("(대기 중인 승인 요청 없음)")
    for r in rows:
        exp = r["expires_at"].strftime("%Y-%m-%d %H:%M") if r["expires_at"] else "-"
        print(f"{str(r['approval_id'])[:8]}  {r['action']}({r['target_pk']})  요청 {r['requested_by']['id']}  "
              f"승인자 {'/'.join(r['approver_roles'])}  기한 {exp}{'  (기한 지남)' if r['overdue'] else ''}  "
              f"변경 {len(r['preview'])}건")
    return 0


def cmd_decisions(args) -> int:
    from .objects import OntologyClient
    with OntologyClient.open(args.file, args.db_url) as client:
        rows = client.policy.decisions(args.limit)
    if not rows:
        print("(정책 판단 기록 없음)")
    for r in reversed(rows):
        target = f"{r['object_type'] or ''}{'(' + r['target_pk'] + ')' if r['target_pk'] else ''}"
        print(f"{str(r['created_at'])[:19]}  {r['decision']:5s} {r['kind']:8s} {r['actor_id']} ({r['actor_type']})  "
              f"{target}  {r['policy'] or ''}  {json.dumps(r['detail'], ensure_ascii=False)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import Served, create_app, load_api_keys
    if args.registry:
        from .registry import Registry
        with Registry(args.registry_url) as reg:
            served = [Served.from_registry(v) for v in reg.active().values()]
    else:
        served = [Served.from_file(f) for f in args.files]
    if not served:
        raise SystemExit("서빙할 온톨로지가 없습니다 (파일을 주거나 --registry 에 활성 버전을 등록하세요)")
    for s in served:
        print(f"  {s.name} v{s.version} ({s.origin})")
    if args.auth == "jwt":
        from .auth import JwtAuth
        _load_env()
        app = create_app(served, auth=JwtAuth(), today=args.today)
    else:
        if not args.keys:
            raise SystemExit("--auth keys 에는 --keys 파일이 필요합니다")
        app = create_app(served, api_keys=load_api_keys(args.keys), today=args.today)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_sdk(args) -> int:
    from .codegen import generate, metadata_from_file, metadata_from_url
    if args.url:
        if not (args.ontology and args.token):
            raise SystemExit("--url 을 쓰면 --ontology 와 --token 도 필요합니다")
        meta = metadata_from_url(args.url, args.ontology, args.token)
    elif args.file:
        meta = metadata_from_file(args.file)
    else:
        raise SystemExit("정의 파일 또는 --url 이 필요합니다")
    code = generate(meta, class_name=args.class_name)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(code, encoding="utf-8")
        print(f"SDK → {args.output} ({meta['name']} v{meta['version']}, {len(code.splitlines())}줄)")
    else:
        print(code)
    return 0


def cmd_states(args) -> int:
    from .states import StateMachines
    onto = _load_or_exit(args.file)
    sm = StateMachines(onto)
    types = [args.type] if args.type else [n for n, ot in onto.object_types.items() if ot.state_machine]
    for i, name in enumerate(types):
        m = sm.machine(name)
        if m is None:
            print(f"'{name}' 에 상태 머신이 없습니다")
            return 1
        if args.mermaid:
            print(sm.mermaid(name))
            continue
        if i:
            print()
        print(f"# {name}.{m.property}  처음: {', '.join(m.initial)}  ·  종료: {', '.join(m.terminal)}")
        for action, t in m.transitions.items():
            print(f"  {'·'.join(t.from_):12s} → {t.to:8s} {action}")
    return 0


def cmd_available(args) -> int:
    from .objects import OntologyClient
    from .query import QueryError
    with OntologyClient.open(args.file, args.db_url, today=args.today) as client:
        try:
            print(client.actions.available(args.type, args.pk, actor=_actor(args.actor)))
        except QueryError as e:
            print(f"오류: {e}")
            return 1
    return 0


def cmd_history(args) -> int:
    from .objects import OntologyClient
    with OntologyClient.open(args.file, args.db_url) as client:
        rows = client.actions.history(args.type, args.pk)
    if not rows:
        print("(상태 전이 기록 없음)")
    for r in rows:
        print(f"{str(r['created_at'])[:19]}  {r['from_state'] or '(생성)':8s} → {r['to_state']:8s} "
              f"{r['action']:26s} {r['actor_id']} ({r['actor_type']})")
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
    sp.add_argument("--as", dest="as_", metavar="ACTOR", help="이 실행자의 권한으로 조회: 아이디,역할,유형 (21장)")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("token", help="개발용 토큰(JWT) 발급 (21장)")
    tsub = sp.add_subparsers(dest="op", required=True)
    t = tsub.add_parser("issue")
    t.add_argument("--sub", required=True, help="실행자 아이디")
    t.add_argument("--role", required=True)
    t.add_argument("--type", required=True, choices=["human", "agent", "system"])
    t.add_argument("--scope", required=True, help="쉼표 구분: objects:read,functions:call,actions:apply,approvals:decide")
    t.add_argument("--ttl", default="1h", help="유효 기간 (30m · 1h · 7d)")
    t.add_argument("--show", action="store_true", help="토큰 내용을 표준 오류로 함께 출력")
    sp.set_defaults(func=cmd_token)

    sp = sub.add_parser("approvals", help="승인 대기 목록 · 기한 지난 요청 만료 (21장)")
    sp.add_argument("op", choices=["list", "expire"])
    sp.add_argument("file")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_approvals)

    sp = sub.add_parser("decisions", help="정책 판단 기록 (21장)")
    sp.add_argument("file")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_decisions)

    sp = sub.add_parser("call", help="Function 호출")
    sp.add_argument("file")
    sp.add_argument("function")
    sp.add_argument("-a", "--arg", action="append", metavar="KEY=VALUE", help="입력 (object 는 기본키)")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_call)

    sp = sub.add_parser("run", help="Action 실행 (기본 dry-run)")
    sp.add_argument("file")
    sp.add_argument("action")
    sp.add_argument("target", help="대상 객체의 기본키")
    sp.add_argument("--actor", required=True, help="아이디,역할,유형 (예: agent-01,AI비서,agent)")
    sp.add_argument("-a", "--arg", action="append", metavar="KEY=VALUE", help="파라미터")
    sp.add_argument("--commit", action="store_true", help="실제로 반영 (생략하면 미리보기 후 롤백)")
    sp.add_argument("--expect", action="append", metavar="KEY=VALUE",
                    help="읽어 둔 값 (예: status=검토중). 그사이 바뀌었으면 실행하지 않음")
    sp.add_argument("--idempotency-key", help="멱등 키: 같은 키로 다시 실행하면 처음 결과를 돌려줌 (22장)")
    sp.add_argument("--retries", type=int, default=0, help="일시적 실패 시 다시 시도할 횟수 (22장)")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("states", help="상태 머신 보기")
    sp.add_argument("file")
    sp.add_argument("type", nargs="?", help="객체 타입 (생략하면 전부)")
    sp.add_argument("--mermaid", action="store_true", help="Mermaid 상태 다이어그램으로 출력")
    sp.set_defaults(func=cmd_states)

    sp = sub.add_parser("available", help="지금 이 객체에 할 수 있는 Action")
    sp.add_argument("file")
    sp.add_argument("type")
    sp.add_argument("pk")
    sp.add_argument("--actor", required=True, help="아이디,역할,유형")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_available)

    sp = sub.add_parser("history", help="객체의 상태 전이 이력")
    sp.add_argument("file")
    sp.add_argument("type")
    sp.add_argument("pk")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("registry", help="Ontology Registry: 버전 등록·비교·활성화")
    rsub = sp.add_subparsers(dest="op", required=True)
    r = rsub.add_parser("register", help="정의를 draft 로 등록")
    r.add_argument("file")
    r.add_argument("--actor", required=True, help="아이디,유형 (예: A001,human)")
    r.add_argument("--notes")
    r = rsub.add_parser("activate", help="버전을 운영에 반영 (retired 버전이면 롤백)")
    r.add_argument("name")
    r.add_argument("version")
    r.add_argument("--actor", required=True)
    r.add_argument("--allow-breaking", action="store_true", help="기존 사용자를 깨는 변경 허용")
    r.add_argument("--no-db-check", action="store_true", help="DB 대조 검사 생략")
    r = rsub.add_parser("diff", help="두 버전 비교")
    r.add_argument("name")
    r.add_argument("old")
    r.add_argument("new")
    r = rsub.add_parser("history", help="등록 이력")
    r.add_argument("name", nargs="?")
    for r in rsub.choices.values():
        r.add_argument("--registry-url")
    sp.set_defaults(func=cmd_registry)

    sp = sub.add_parser("serve", help="Ontology API 서버 실행 (FastAPI)")
    sp.add_argument("files", nargs="*", help="온톨로지 정의 파일 (개발용)")
    sp.add_argument("--registry", action="store_true", help="Registry 의 활성 버전을 서빙")
    sp.add_argument("--registry-url")
    sp.add_argument("--auth", choices=["keys", "jwt"], default="keys",
                    help="keys: 개발용 키 파일(19장) · jwt: 서명된 토큰(21장, MINIONTO_TOKEN_SECRET)")
    sp.add_argument("--keys", help="개발용 API 키 파일 (YAML)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8800)
    sp.add_argument("--today")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("sdk", help="타입이 있는 Python SDK 생성")
    sp.add_argument("file", nargs="?", help="온톨로지 정의 파일")
    sp.add_argument("--url", help="정의 파일 대신 API 서버 메타데이터 사용")
    sp.add_argument("--ontology")
    sp.add_argument("--token")
    sp.add_argument("--class-name")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_sdk)

    sp = sub.add_parser("schema", help="에디터 자동완성용 JSON Schema 출력")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_schema)
    args = ap.parse_args(argv)
    return args.func(args)
