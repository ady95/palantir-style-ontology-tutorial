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
  python -m minionto approvals decide ontologies/taxos/ontology.yaml <승인ID> --actor A001,세무사,human [--reject]
  python -m minionto decisions ontologies/taxos/ontology.yaml
  python -m minionto outbox list ontologies/taxos/ontology.yaml [--status pending]
  python -m minionto outbox run ontologies/taxos/ontology.yaml --integrations ontologies/taxos/integrations.py \
      --notifier http://127.0.0.1:8900 [--loop]
  python -m minionto outbox requeue ontologies/taxos/ontology.yaml 6
  python -m minionto audit history|trace|verify|metrics ontologies/taxos/ontology.yaml [대상]
  python -m minionto subscriptions run|replay ontologies/taxos/ontology.yaml [구독]
  python -m minionto registry register ontologies/taxos/ontology.yaml --actor A001,human
  python -m minionto registry activate taxos 0.1.0 --actor A005,human
  python -m minionto serve ontologies/taxos/ontology.yaml --keys examples/dev_api_keys.yaml   (또는 --registry)
  python -m minionto sdk ontologies/taxos/ontology.yaml -o build/taxos_sdk.py
  python -m minionto schema [-o schema/minionto.schema.json]
  MINIONTO_TOKEN=<토큰> python -m minionto mcp ontologies/taxos/ontology.yaml            (stdio MCP 서버, 26장)
  python -m minionto mcp ontologies/taxos/ontology.yaml --http --port 8765              (Streamable HTTP)
  python -m minionto agent pending ontologies/taxos/ontology.yaml
  python -m minionto agent confirm ontologies/taxos/ontology.yaml <세션> <확인토큰> --actor A001,세무사,human
  python -m minionto workflow run ontologies/taxos/ontology.yaml --task W1 --today 2027-01-18 [--llm]  (27장)
  python -m minionto workflow resume|show ontologies/taxos/ontology.yaml --task W1 [--stop]
  python -m minionto mapping check|export|draft ../06-hybrid-ontology/taxos_mapping.yaml [-o 파일] [--db]  (29장)
  python -m minionto reason infer|plan|sync ../06-hybrid-ontology/taxos_mapping.yaml                        (30장)
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
        if args.op == "decide":
            return _decide(client, args)
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


def _decide(client, args) -> int:
    """사람이 승인 요청을 승인·반려한다 (승인 ID 앞부분만 써도 된다)"""
    if not args.id or not args.actor:
        raise SystemExit("approvals decide <파일> <승인ID> --actor <id>,세무사,human [--reject] [--comment ...]")
    ids = [str(r["approval_id"]) for r in client.actions.pending_approvals()
           if str(r["approval_id"]).startswith(args.id)]
    if len(ids) != 1:
        raise SystemExit(f"대기 중인 승인 요청 중 '{args.id}' 로 시작하는 것이 {len(ids)}건입니다")
    res = client.actions.decide(ids[0], _actor(args.actor), approve=not args.reject, comment=args.comment)
    print(res)
    return 0 if res.status in ("completed", "rejected") else 1


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


def cmd_outbox(args) -> int:
    from .objects import OntologyClient
    from .outbox import outbox_rows, requeue
    with OntologyClient.open(args.file, args.db_url, today=args.today) as client:
        if args.op == "list":
            rows = outbox_rows(client, args.status, args.limit)
            if not rows:
                print("(이벤트 없음)")
            for r in rows:
                print(f"#{r['event_id']:<4d} {r['event_type']:28s} {r['object_type']}({r['target_pk']})  "
                      f"{r['status']:9s} 시도 {r['attempts']}" + (f"  {r['last_error']}" if r["last_error"] else ""))
            return 0
        if args.op == "requeue":
            ok = requeue(client, args.event_id)
            print(f"#{args.event_id} 다시 보낼 차례로: {'완료' if ok else 'dead 상태인 이벤트가 아닙니다'}")
            return 0 if ok else 1
        # run: 연동 모듈(integrations.py)의 make_dispatcher·Notifier 로 작업자를 만든다
        import importlib.util
        import time

        import httpx
        if not (args.integrations and args.notifier):
            raise SystemExit("run 에는 --integrations 와 --notifier 가 필요합니다")
        spec = importlib.util.spec_from_file_location("minionto_integrations", args.integrations)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        dispatcher = mod.make_dispatcher(client, mod.Notifier(httpx.Client(base_url=args.notifier, timeout=10)),
                                         backoff=args.backoff, max_attempts=args.max_attempts)
        while True:
            for o in dispatcher.run_until_idle(timeout=args.timeout):
                print(o)
            if not args.loop:
                return 0
            time.sleep(args.interval)


def cmd_audit(args) -> int:
    from .audit import AuditLog
    from .objects import OntologyClient
    with OntologyClient.open(args.file, args.db_url) as client:
        audit = AuditLog(client)
        if args.op == "history":
            if len(args.ref) != 2:
                raise SystemExit("history 에는 객체 타입과 기본키가 필요합니다 (예: TaxReturn R00006)")
            rows = audit.object_history(*args.ref)
            print("\n".join(map(str, rows)) if rows else "(기록 없음)")
        elif args.op == "trace":
            if len(args.ref) != 1:
                raise SystemExit("trace 에는 execution_id 가 필요합니다")
            print(json.dumps(audit.trace(args.ref[0]), ensure_ascii=False, indent=2, default=str))
        elif args.op == "verify":
            found = audit.verify()
            print("\n".join(map(str, found)) if found else "점검 통과: 해시 체인·상태 전이·이벤트 출처·엔진 밖 변경 모두 이상 없음")
            return 1 if found else 0
        else:
            m = audit.metrics()
            for key, rows in m.items():
                print(f"# {key}")
                for r in (rows if isinstance(rows, list) else [rows]):
                    print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
    return 0


def cmd_subscriptions(args) -> int:
    from .objects import OntologyClient
    from .subscriptions import SubscriptionRunner
    with OntologyClient.open(args.file, args.db_url, today=args.today) as client:
        runner = SubscriptionRunner(client)
        if args.op == "replay":
            if not args.name:
                raise SystemExit("replay 에는 구독 이름이 필요합니다")
            print(f"{args.name}: 처리 기록 {runner.replay(args.name, args.from_event)}건을 지웠습니다 (다음 run 에서 다시 처리)")
            return 0
        rows = runner.run_once()
        print("\n".join(map(str, rows)) if rows else "(처리할 이벤트 없음)")
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


def cmd_mcp(args) -> int:
    _load_env()
    if args.http:
        import uvicorn

        from .mcp_server import MiniOntoMCP
        app = MiniOntoMCP(args.file, today=args.today, confirm=args.confirm).http_app(host=args.host)
        print(f"MCP 서버 (Streamable HTTP): http://{args.host}:{args.port}/mcp", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    else:
        from .mcp_server import stdio_main
        stdio_main(args.file, today=args.today, confirm=args.confirm)     # stdout 은 MCP 메시지 전용
    return 0


def cmd_agent(args) -> int:
    """사람이 에이전트의 확인 대기 실행을 보고 확인·거절한다 (26장) — 에이전트의 도구가 아니다"""
    from .agent import SessionStore
    from .objects import OntologyClient
    _load_env()
    with OntologyClient.open(args.file, today=args.today) as c:
        store = SessionStore(c)
        if args.cmd == "pending":
            rows = store.pending()
            for r in rows:
                print(f"{r['session']}  {r['token']}  {r['actor']}  {r['action']}({r['target']}) {r['params'] or ''}")
            print(f"확인 대기 {len(rows)}건")
            return 0
        actor = _actor(args.actor)
        if actor.get("type") != "human":
            raise SystemExit("확인은 사람(human)만 할 수 있습니다")
        row = c.conn.execute("SELECT actor FROM minionto.agent_sessions WHERE session_key = %s",
                             (args.session,)).fetchone()
        if row is None:
            raise SystemExit(f"세션 '{args.session}' 이 없습니다")
        s = store.open(args.session, {**row["actor"]}, confirm=True)
        out = s.confirm(args.token, approve=not args.reject, by=actor)
        store.save(args.session, s)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["status"] in ("completed", "rejected") else 1


def _wf_line(node: str, upd: dict) -> str | None:
    """그래프 진행을 한 줄로 (노드가 남긴 log 로)"""
    for x in (upd or {}).get("log", []):
        if node == "detect":
            return (f"detect         자료수집중 신고 {x['returns']}건 · 고객 {x['customers']}명 → "
                    f"대상 {x['targets']}건")
        if node == "analyze":
            return f"analyze  {x['return_id']}  판단 {x['check']}"
        if node == "draft":
            return f"draft    {x['return_id']}  메모 {x['by']}" + (f"  (검사 {x['problems']})" if x["problems"] else "")
        if node == "act":
            return f"act      {x['return_id']}  {x['status']}"
        if node == "wait_approval":
            return f"wait_approval  승인 대기 {x['waiting']}건"
        if node == "verify":
            return f"verify         확인 문제 {x['problems']}건"
    return None


def _wf_stream(graph, inp, cfg, verbose: bool) -> None:
    for chunk in graph.stream(inp, cfg, stream_mode="updates"):
        for node, upd in chunk.items():
            if node == "__interrupt__":
                continue
            line = _wf_line(node, upd if isinstance(upd, dict) else {})
            if line and (verbose or not line.startswith("analyze")):
                print(line)


def _wf_finish(graph, cfg) -> int:
    st = graph.get_state(cfg)
    v = st.values
    waits = [i.value for t in st.tasks for i in t.interrupts]
    if waits:
        w = waits[0]
        print(f"\n멈춤: {w['message']} (업무 {w['task_id']})")
        for x in w["waiting"]:
            print(f"  승인 {x['approval_id'][:8]}  {x['return_id']} ({x['customer_id']})  메모: {x['note']}")
        print("승인·반려는 사람이: python -m minionto approvals decide <파일> <승인ID> --actor <id>,세무사,human")
        print(f"이어 가기: python -m minionto workflow resume <파일> --task {w['task_id']}")
        return 0
    r = v.get("report") or {}
    print(f"\n보고 (기준일 {r.get('as_of')}, 점수 {r.get('min_score')} 이상 {r.get('targets')}건)")
    print("  결과    " + ", ".join(f"{k} {n}" for k, n in r.get("counts", {}).items()))
    print("  완료    " + ", ".join(r.get("completed", [])))
    print("  승인    " + ", ".join(f"{k} {s}" for k, s in r.get("approval", {}).items()))
    for why, ids in r.get("handoff_by_reason", {}).items():
        print(f"  넘김 {len(ids):2d}건  {why}")
    print("  확인 실패 " + (", ".join(r.get("unverified", [])) or "없음"))
    print("\n요약:\n" + (v.get("summary") or ""))
    return 0


def cmd_workflow(args) -> int:
    """신고 지연 위험 고객 처리 업무 그래프 (27장)"""
    from .db import database_url
    from .loader import load
    from .objects import OntologyClient
    from .workflow import FilingRiskWorkflow, LLMWriter, TemplateWriter, config, postgres_saver
    _load_env()
    saver = postgres_saver(database_url(load(args.file), args.db_url))
    cfg = config(args.task)
    prev = saver.get_tuple(cfg)
    if args.op == "run" and prev is not None:
        raise SystemExit(f"업무 '{args.task}' 가 이미 있습니다. 이어 가려면 resume, 새로 하려면 다른 --task")
    if args.op != "run" and prev is None:
        raise SystemExit(f"업무 '{args.task}' 가 없습니다")
    as_of = args.today if args.op == "run" else prev.checkpoint["channel_values"].get("as_of")
    with OntologyClient.open(args.file, args.db_url, today=as_of) as client:
        writer = TemplateWriter()
        if args.llm:
            from .llm import ChatClient
            writer = LLMWriter(ChatClient())
        graph = FilingRiskWorkflow(client, _actor(args.actor), writer=writer,
                                   min_score=args.min_score).build(checkpointer=saver)
        if args.op == "show":
            hist = list(graph.get_state_history(cfg))
            print(f"체크포인트 {len(hist)}개 · 다음 노드 {graph.get_state(cfg).next}")
            return _wf_finish(graph, cfg)
        if args.op == "run":
            inp = {"task_id": args.task, "as_of": as_of, "min_score": args.min_score}
        else:
            from langgraph.types import Command
            inp = Command(resume={"stop": True} if args.stop else "check")
        _wf_stream(graph, inp, cfg, args.verbose)
        if args.llm:
            print(f"\nLLM 사용: {json.dumps(writer.usage)}")
        return _wf_finish(graph, cfg)


def cmd_reason(args) -> int:
    """OWL 추론 → 고객 분류(CustomerSegment) 동기화 (30장). plan 은 바꾸지 않고, sync 는 시스템 Action 으로 반영"""
    from collections import Counter

    from .objects import OntologyClient
    from .reasoning import infer, plan_segments, sync_segments
    from .semantic import Mapping
    _load_env()
    m = Mapping.load(args.mapping)
    with OntologyClient(m.onto, args.db_url, today=args.today) as c:
        inf = infer(c, m)
        print(f"{inf.source} · 트리플 {inf.triples[0]:,} → {inf.triples[1]:,} · {inf.seconds}초 · 모순 {len(inf.errors)}건")
        for cls, n in inf.count().items():
            print(f"  {m.short(cls):26s} {n:4d}  → {m.inferred[cls]['segment']}")
        if args.op == "infer":
            return 0
        if args.op == "plan":
            plan = plan_segments(c, m, inf)
            for p in plan:
                if p["op"] != "assign" or args.verbose:
                    print(f"  {p['op']:7s} {p['customer']} {p['segment']} {p.get('why', '')}")
            print("계획: " + (", ".join(f"{k} {v}" for k, v in Counter(p["op"] for p in plan).items()) or "할 일 없음"))
            return 0
        res = sync_segments(c, m, inf)
        print("반영: " + (", ".join(f"{k} {v}" for k, v in Counter(r["status"] for r in res).items()) or "할 일 없음"))
        return 0 if all(r["status"] in ("completed", "skip", "keep") for r in res) else 1


def cmd_mapping(args) -> int:
    """의미 모델(OWL) ↔ 운영형 정의 매핑: 검증 · RDF 내보내기 · OWL 에서 정의 초안 (29장)"""
    from .semantic import Mapping, axiom_checks, check, draft_from_owl, draft_yaml, export_rdf
    m = Mapping.load(args.mapping)
    if args.op == "check":
        rep = check(m)
        for k, (mapped, excluded, total) in rep.coverage.items():
            print(f"{k:10s} 매핑 {mapped} · 이유 있는 제외 {excluded} · 전체 {total}")
        for f in rep.findings:
            print(f"[{f.level}] {f.side}: {f.message}")
        if args.db:
            from .objects import OntologyClient
            with OntologyClient(m.onto, args.db_url) as c:
                for a in axiom_checks(c, m):
                    print(f"[공리] {a['axiom']} — 위반 {a['violations']}건")
        print(f"오류 {len(rep.errors)}건")
        return 1 if rep.errors else 0
    if args.op == "draft":
        text = draft_yaml(draft_from_owl(m.owl))
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"초안 → {args.output}")
        else:
            print(text)
        return 0
    from .objects import OntologyClient
    _load_env()
    with OntologyClient(m.onto, args.db_url) as c:
        g = export_rdf(c, m, actor=_actor(args.actor) if args.actor else None)
    out = args.output or "export.ttl"
    g.serialize(out, format="turtle")
    print(f"트리플 {len(g):,}개 → {out}")
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

    sp = sub.add_parser("approvals", help="승인 대기 목록 · 승인/반려 · 기한 지난 요청 만료 (21장)")
    sp.add_argument("op", choices=["list", "expire", "decide"])
    sp.add_argument("file")
    sp.add_argument("id", nargs="?", help="decide: 승인 ID (앞부분만 써도 된다)")
    sp.add_argument("--actor", help="decide: 승인하는 사람 id,역할,human")
    sp.add_argument("--reject", action="store_true")
    sp.add_argument("--comment")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_approvals)

    sp = sub.add_parser("outbox", help="아웃박스 이벤트 보기 · 발송 작업자 실행 · dead 다시 보내기 (23장)")
    sp.add_argument("op", choices=["list", "run", "requeue"])
    sp.add_argument("file")
    sp.add_argument("event_id", nargs="?", type=int, help="requeue 할 이벤트 번호")
    sp.add_argument("--status", choices=["pending", "published", "dead"])
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--integrations", help="연동 모듈 (make_dispatcher·Notifier 를 가진 .py)")
    sp.add_argument("--notifier", help="외부 발송 서비스 주소")
    sp.add_argument("--backoff", type=float, default=1.0, help="첫 재시도 간격(초), 시도마다 두 배")
    sp.add_argument("--max-attempts", type=int, default=5)
    sp.add_argument("--timeout", type=float, default=30.0, help="한 번 실행에서 기다릴 최대 시간(초)")
    sp.add_argument("--loop", action="store_true", help="계속 실행 (Ctrl+C 로 멈춤)")
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_outbox)

    sp = sub.add_parser("audit", help="감사 로그: 객체 이력 · 실행 추적 · 완전성 점검 · 지표 (24장)")
    sp.add_argument("op", choices=["history", "trace", "verify", "metrics"])
    sp.add_argument("file")
    sp.add_argument("ref", nargs="*", help="history: 객체 타입 기본키 · trace: execution_id")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("subscriptions", help="이벤트 구독 실행 · 재처리 (24장)")
    sp.add_argument("op", choices=["run", "replay"])
    sp.add_argument("file")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--from-event", type=int, default=0)
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_subscriptions)

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

    sp = sub.add_parser("mcp", help="MCP 서버 실행 — stdio(MINIONTO_TOKEN) 또는 --http (26장)")
    sp.add_argument("file")
    sp.add_argument("--http", action="store_true", help="Streamable HTTP (요청마다 Bearer 토큰)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--confirm", action="store_true", help="run_action 은 미리보기만, 실행은 사람이 agent confirm")
    sp.add_argument("--today")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("agent", help="에이전트 확인 대기 보기 · 확인/거절 (26장)")
    sp.add_argument("cmd", choices=["pending", "confirm"])
    sp.add_argument("file")
    sp.add_argument("session", nargs="?")
    sp.add_argument("token", nargs="?")
    sp.add_argument("--actor", help="확인하는 사람 id,역할,human")
    sp.add_argument("--reject", action="store_true")
    sp.add_argument("--today")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("workflow", help="신고 지연 위험 고객 처리 업무 그래프 — 실행·재개·상태 (27장)")
    sp.add_argument("op", choices=["run", "resume", "show"])
    sp.add_argument("file")
    sp.add_argument("--task", required=True, help="업무 ID (그래프 thread_id = 도구 세션 키)")
    sp.add_argument("--actor", default="agent-01,AI비서,agent")
    sp.add_argument("--min-score", type=int, default=50)
    sp.add_argument("--llm", action="store_true", help="메모·요약을 OpenAI 호환 LLM 으로")
    sp.add_argument("--stop", action="store_true", help="resume: 승인을 더 기다리지 않고 보고")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_workflow)

    sp = sub.add_parser("mapping", help="OWL ↔ 운영형 정의 매핑: 검증·RDF 내보내기·정의 초안 (29장)")
    sp.add_argument("op", choices=["check", "export", "draft"])
    sp.add_argument("mapping", help="매핑 파일 (예: ../06-hybrid-ontology/taxos_mapping.yaml)")
    sp.add_argument("-o", "--output")
    sp.add_argument("--actor", help="export: 이 실행자의 권한으로 (id,역할,유형)")
    sp.add_argument("--db", action="store_true", help="check: 공리를 DB 데이터로도 확인")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_mapping)

    sp = sub.add_parser("reason", help="OWL 추론 → 고객 분류 동기화: infer·plan·sync (30장)")
    sp.add_argument("op", choices=["infer", "plan", "sync"])
    sp.add_argument("mapping")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.add_argument("--today")
    sp.add_argument("--db-url")
    sp.set_defaults(func=cmd_reason)

    sp = sub.add_parser("schema", help="에디터 자동완성용 JSON Schema 출력")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_schema)
    args = ap.parse_args(argv)
    return args.func(args)
