"""35장. 같은 범용 기능을 세 도메인에 — 도메인 지식 없이 정의(메타데이터)만 보고 돌린다

    uv run python 10-benchmarks/reuse/smoke.py              # 표 출력
    uv run python 10-benchmarks/reuse/smoke.py --json       # results/smoke.json 에도 저장

기능마다 OK / FAIL(이유) / 해당 없음 을 낸다. 마지막 '다른 도메인 단어'는 에이전트·API·SDK 에 나가는 글에
다른 도메인의 이름(객체·링크·Function·Action 이름, 온톨로지 표시 이름의 낱말)이 섞여 있는지 찾는다. 모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ENGINE = ROOT / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import OntologyClient  # noqa: E402
from minionto.validator import validate  # noqa: E402

DOMAINS = ["taxos", "northwind", "maintenance"]
TODAY = {"taxos": "2027-01-18", "northwind": "2026-12-01", "maintenance": "2027-01-18"}
STOP = {"온톨로지", "운영"}


def path_of(d: str) -> Path:
    return ENGINE / "ontologies" / d / "ontology.yaml"


def vocabulary(onto) -> set[str]:
    """도메인 고유 단어: 객체 타입·링크·Function·Action 이름(식별자)과 온톨로지 표시 이름의 낱말"""
    words = set(onto.object_types) | set(onto.functions) | set(onto.actions)
    for lt in onto.link_types.values():
        words |= {lt.forward_name, lt.reverse_name}
    words |= {w for w in re.split(r"[\s·]+", onto.ontology.display_name or "") if len(w) > 1}
    return {w for w in words if w not in STOP}


def own_words(onto) -> set[str]:
    """자기 도메인에서 쓰는 이름 전체 (속성 이름 포함) — 다른 도메인과 겹치면 섞인 것으로 보지 않는다"""
    return vocabulary(onto) | {p for ot in onto.object_types.values() for p in ot.properties}


def leaks(text: str, foreign: set[str]) -> list[str]:
    found = []
    for w in sorted(foreign):
        pat = rf"(?<![A-Za-z0-9_]){re.escape(w)}(?![A-Za-z0-9_])" if w.isascii() else re.escape(w)
        if re.search(pat, text):
            found.append(w)
    return found


def first_pk(c, otype: str):
    o = c.objects(otype).first()
    return o.pk if o else None


def sample_args(c, fn) -> dict:
    args = {}
    for name, p in fn.inputs.items():
        if p.type == "object":
            args[name] = first_pk(c, p.object_type)
        elif p.default is not None or not p.required:
            continue
        else:
            args[name] = {"integer": 1, "decimal": 1, "float": 1.0, "boolean": True}.get(p.type, "x")
    return args


def run_domain(d: str, vocab: dict[str, set[str]], own: set[str]) -> dict:
    from fastapi.testclient import TestClient

    from minionto.agent import AgentSession
    from minionto.api import Served, create_app
    from minionto.audit import AuditLog
    from minionto.codegen import generate
    from minionto.describe import public_metadata
    from minionto.llm import SYSTEM_PROMPT
    from minionto.mcp_server import GUIDELINES, MiniOntoMCP
    from minionto.auth import SCOPES, Principal
    from minionto.states import StateMachines

    out: dict[str, str] = {}
    texts: dict[str, str] = {}

    def step(name: str, fn):
        try:
            r = fn()
            out[name] = "OK" + (f" ({r})" if r not in (None, "") else "")
        except Exception as e:                      # 실패도 결과다 — 이유를 남긴다
            out[name] = f"FAIL {type(e).__name__}: {str(e)[:90]}"

    with OntologyClient.open(path_of(d), today=TODAY[d]) as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        o = c.onto
        roles = o.roles
        humans = [{"id": f"{d}-{r}", "role": r, "type": "human"} for r in roles if r not in ("AI비서", "시스템")]
        agent = {"id": f"{d}-agent", "role": "AI비서", "type": "agent"} if "AI비서" in roles else None

        def f_validate():
            from minionto.db import check_database, database_url
            rep = check_database(o, database_url(o), validate(o, path_of(d).parent))
            errs = [i for i in rep.issues if i.level == "error"]
            if errs:
                raise RuntimeError(f"오류 {len(errs)}: {errs[0].message}")
            return f"객체 {len(o.object_types)}·링크 {len(o.link_types)}·Function {len(o.functions)}·Action {len(o.actions)}"
        step("정의 검증·DB 대조", f_validate)

        meta = public_metadata(o)
        texts["API 메타데이터"] = json.dumps(meta, ensure_ascii=False)

        def f_sdk():
            code = generate(meta)
            compile(code, f"{d}_sdk.py", "exec")
            texts["SDK"] = code
            return f"{len(code.splitlines())}줄"
        step("SDK 생성·컴파일", f_sdk)

        step("객체 조회", lambda: f"{sum(c.objects(t).count() for t in o.object_types):,}행")

        def f_links():
            n = 0
            for lt in o.link_types.values():
                pk = first_pk(c, lt.from_)
                if pk is not None:
                    ot = o.object_types[lt.from_]
                    c.objects(lt.from_).where(**{ot.source.primary_key: pk}).pivot(lt.forward_name).count()
                    n += 1
            return f"{n}/{len(o.link_types)}"
        step("링크 따라가기", f_links)

        def f_functions():
            for name, fn in o.functions.items():
                c.functions.call(name, **sample_args(c, fn))
            return f"{len(o.functions)}개"
        step("Function 호출", f_functions)

        def f_states():
            sm = StateMachines(o)
            names = [t for t in o.object_types if sm.machine(t)]
            for t in names:
                sm.mermaid(t)
            return ", ".join(names) if names else "상태 머신 없음"
        step("상태 머신", f_states)

        def f_available():
            n = 0
            for name, act in o.actions.items():
                pk = first_pk(c, act.target)
                if pk is None:
                    continue
                for h in humans:
                    c.actions.check(name, pk, actor=h)
                    n += 1
            return f"판단 {n}회"
        step("Action 판단(check)", f_available)

        def f_dry():
            st = {}
            for name, act in o.actions.items():
                if not act.parameters and first_pk(c, act.target) is not None:
                    with c.conn.transaction(force_rollback=True):
                        r = c.actions.apply(name, first_pk(c, act.target), actor=humans[0])
                        st[r.status] = st.get(r.status, 0) + 1
            return ", ".join(f"{k} {v}" for k, v in sorted(st.items())) or "입력 없는 Action 없음"
        step("Action 실행(입력 없는 것)", f_dry)

        def f_policy():
            if not (o.security.row_policies or o.security.property_policies):
                return "정책 없음"
            seen = {}
            for h in humans + ([agent] if agent else []):
                t = next(iter(o.security.row_policies.values())).rules if o.security.row_policies else {}
                otype = next(iter(t), next(iter(o.object_types)))
                seen[h["role"]] = c.objects(otype).secure(h).count()
            return " · ".join(f"{k} {v}" for k, v in seen.items())
        step("권한 정책", f_policy)

        if agent:
            def f_agent():
                s = AgentSession(c, agent)
                texts["에이전트 컨텍스트"] = s.context_text()
                texts["LLM 시스템 프롬프트"] = SYSTEM_PROMPT
                return f"도구 {len(s.tools())}개, 컨텍스트 {len(texts['에이전트 컨텍스트']):,}자"
            step("에이전트 도구", f_agent)

            def f_mcp():
                m = MiniOntoMCP(path_of(d), today=TODAY[d], client=c)
                p = Principal(agent, frozenset(SCOPES), "api_key")
                tools = m.list_tools(p)
                texts["MCP 도구 설명"] = "\n".join(t.description for t in tools)
                texts["MCP 지침"] = GUIDELINES
                return f"도구 {len(tools)}개"
            step("MCP 도구", f_mcp)
        else:
            out["에이전트 도구"] = out["MCP 도구"] = "해당 없음 (AI 역할 없음)"

        def f_api():
            app = create_app([Served.from_file(path_of(d))], api_keys={"k": humans[0]}, today=TODAY[d])
            with TestClient(app) as tc:
                r = tc.get(f"/v1/ontologies/{o.ontology.name}", headers={"Authorization": "Bearer k"})
                r.raise_for_status()
                first = next(iter(o.object_types))
                r2 = tc.post(f"/v1/ontologies/{o.ontology.name}/objects/{first}/search",
                             json={"limit": 3}, headers={"Authorization": "Bearer k"})
                r2.raise_for_status()
            return f"HTTP {r.status_code}·{r2.status_code}"
        step("REST API", f_api)

        step("감사 로그 점검", lambda: f"문제 {len(AuditLog(c).verify())}건")

    foreign = set().union(*(v for k, v in vocab.items() if k != d)) - own
    leak = {k: leaks(t, foreign) for k, t in texts.items()}
    out["다른 도메인 단어"] = "없음" if not any(leak.values()) else \
        "; ".join(f"{k}: {', '.join(v[:6])}" for k, v in leak.items() if v)
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from minionto.loader import load
    ontos = {d: load(path_of(d)) for d in DOMAINS}
    vocab = {d: vocabulary(o) for d, o in ontos.items()}
    results = {d: run_domain(d, vocab, own_words(ontos[d])) for d in DOMAINS}
    for d, r in results.items():
        print(f"\n== {d}")
        for k, v in r.items():
            print(f"  {k:24s} {v}")
    if "--json" in sys.argv:
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / "smoke.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
