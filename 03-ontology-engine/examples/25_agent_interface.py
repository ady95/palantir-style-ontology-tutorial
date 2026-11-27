"""25장. AI 에이전트를 위한 Object · Action 인터페이스

    uv run python 03-ontology-engine/examples/25_agent_interface.py          # 1~4단계 (LLM 없이)
    uv run python 03-ontology-engine/examples/25_agent_interface.py --llm    # 5단계까지 (OpenAI 호환 API)

1. 컨텍스트    — 전체 메타데이터 vs 이 실행자에게 필요한 만큼 (글자 수, --llm 이면 토큰 수)
2. 도구 목록   — 범용 도구 6개 / Action 마다 도구
3. 도구 직접 호출 — 읽기 전 실행 차단, 판단, 미리보기, 실행, 재전송, 감춘 속성, 동시 변경
4. 확인 모드   — 에이전트는 미리보기까지, 실행은 사람이 확인한 뒤
5. LLM 에이전트 — 같은 도구로 실제 LLM 이 업무 요청을 처리한다

모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402
from minionto.describe import public_metadata  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def short(out: dict, width: int = 150) -> str:
    keep = {k: v for k, v in out.items() if k not in ("next_step", "execution_id", "rows", "object", "links")}
    if "object" in out:                                  # get_object: 키와 상태만
        o = out["object"]
        keep["object"] = {k: v for k, v in o.items() if k in ("return_id", "customer_id", "status", "risk_level")}
    text = json.dumps(keep, ensure_ascii=False)
    return text if len(text) <= width else text[:width] + " …"


def show(s: AgentSession, tool: str, args: dict) -> dict:
    out = s.call(tool, args)
    print(f"{tool}({', '.join(f'{k}={v}' for k, v in args.items())})")
    print(f"  → {short(out)}")
    if out.get("next_step"):
        print(f"    next_step: {out['next_step']}")
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    use_llm = "--llm" in sys.argv
    with OntologyClient.open(TAXOS, today=AS_OF) as onto, onto.conn.transaction(force_rollback=True):
        a = onto.actions
        normal = a.candidates(REQ, actor=AI).where(
            {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk
        risky = a.candidates(REQ, actor=AI).where(
            {"field": "customer.risk_level", "op": "eq", "value": "높음"}).order_by("return_id").first().pk
        ready = a.candidates("start_review", actor=AI).order_by("return_id").first().pk

        # ------------------------------------------------------------------ 1
        section("1. 컨텍스트 — 에이전트에게 무엇을 보여 줄까")
        s = AgentSession(onto, AI, task_id="demo")
        variants = {
            "전체 공개 메타데이터 (19장, JSON)": json.dumps(public_metadata(onto.onto), ensure_ascii=False),
            "에이전트 컨텍스트 (JSON)": json.dumps(s.context(), ensure_ascii=False),
            "에이전트 컨텍스트 (텍스트)": s.context_text(),
            "도구 정의 — 범용 6개": json.dumps(s.tools("generic"), ensure_ascii=False),
            "도구 정의 — Action 마다": json.dumps(s.tools("per_action"), ensure_ascii=False),
        }
        chat = None
        if use_llm:
            from minionto.llm import ChatClient
            chat = ChatClient()
        for name, text in variants.items():
            tok = f"  토큰 {chat.count_tokens(text):>5,}" if chat else ""
            print(f"{name:34s} 글자 {len(text):>6,}{tok}")
        ctx = s.context()
        print(f"\n실행할 수 있는 Action: {', '.join(ctx['actions'])}  (전체 {len(onto.onto.actions)}개 중)")
        print(f"감춘 속성: " + ", ".join(f"{t}.{p}" for t, d in ctx["object_types"].items() for p in d.get("hidden", [])))
        print("\n텍스트 컨텍스트 중 Action 부분:")
        text = s.context_text()
        print("\n".join("  " + ln for ln in text[text.index("# 실행할 수 있는"):].splitlines()))

        # ------------------------------------------------------------------ 2
        section("2. 도구 목록")
        for style in ("generic", "per_action"):
            names = [t["function"]["name"] for t in s.tools(style)]
            print(f"{style:11s} {len(names)}개: {', '.join(names)}")
        run = next(t for t in s.tools() if t["function"]["name"] == "run_action")["function"]
        print("run_action.action enum:", run["parameters"]["properties"]["action"]["enum"])

        with onto.conn.transaction(force_rollback=True):        # 3·4단계 변경은 여기서 되돌린다
            # ------------------------------------------------------------------ 3
            section(f"3. 도구 직접 호출 — {normal}")
            show(s, "run_action", {"action": REQ, "target": normal})
            show(s, "get_object", {"object_type": "TaxReturn", "id": normal})
            show(s, "list_actions", {"object_type": "TaxReturn", "id": normal})
            show(s, "check_action", {"action": REQ, "target": normal})
            show(s, "run_action", {"action": REQ, "target": normal, "dry_run": True})
            show(s, "run_action", {"action": REQ, "target": normal})
            show(s, "run_action", {"action": REQ, "target": normal})
            show(s, "run_action", {"action": "complete_filing", "target": normal})
            show(s, "find_objects", {"object_type": "Customer",
                                     "where": [{"field": "phone", "op": "not_null"}]})
            cust = onto.get("TaxReturn", risky).customer_id
            print(f"\n고위험 고객 {cust} 의 신고 {risky}:")
            show(s, "get_object", {"object_type": "TaxReturn", "id": risky})
            show(s, "run_action", {"action": REQ, "target": risky})
            show(s, "run_action", {"action": REQ, "target": risky})
            print(f"\n동시 변경 — 에이전트가 {ready} 를 읽은 뒤 세무사가 먼저 검토를 시작:")
            show(s, "get_object", {"object_type": "TaxReturn", "id": ready})
            print(f"  (세무사 start_review → {a.apply('start_review', ready, actor=TAX).status})")
            show(s, "run_action", {"action": "start_review", "target": ready})
            print("\n도구 호출 기록:", ", ".join(f"{c['tool']}:{c['status']}" for c in s.calls))

            # ------------------------------------------------------------------ 4
            section("4. 확인 모드 — 실행은 사람이 확인한 뒤")
            cs = AgentSession(onto, AI, task_id="confirm-demo", confirm=True)
            nxt = a.candidates(REQ, actor=AI).where(
                {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk
            show(cs, "get_object", {"object_type": "TaxReturn", "id": nxt})
            out = show(cs, "run_action", {"action": REQ, "target": nxt})
            for c in out.get("changes", []):
                print("    미리보기:", c["op"], c["object_type"], c["pk"], c.get("changed") or "")
            print(f"  (사람이 확인 → {short(cs.confirm(out['token']))})")
            print("에이전트 도구 목록에 confirm 이 있는가:",
                  any(t["function"]["name"] == "confirm" for t in cs.tools()))

        # ------------------------------------------------------------------ 5
        if not use_llm:
            print("\n(5단계는 --llm 으로 실행합니다)")
            return 0
        from minionto.llm import run_agent
        section(f"5. LLM 에이전트 — 모델 {chat.model}")
        repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
        truth = onto.objects("Customer").where({"field": "risk_level", "op": "eq", "value": "높음"}).where(
            {"some": {"link": "tax_returns", "where": {"field": "status", "op": "eq", "value": "자료수집중"}}}).count()
        cases = [
            ("자료 요청", f"{normal} 신고에 빠진 자료를 고객에게 요청해 줘."),
            ("고위험 고객", f"{onto.get('TaxReturn', risky).customer_id} 고객의 자료수집중인 신고에 빠진 자료를 요청해 줘."),
            ("권한 밖", f"{normal} 신고를 신고 완료로 처리해 줘."),
            ("규칙상 불가", f"{normal} 신고서 검토를 시작해 줘."),
            ("조회", "자료수집중인 신고가 있는 고객 중 위험 등급이 높음인 고객은 몇 곳이야?"),
        ]
        for title, req in cases:
            print(f"\n## {title}: {req}" + (f"  (엔진 집계 정답: {truth})" if title == "조회" else ""))
            for i in range(repeat):
                detail = i == 0
                with onto.conn.transaction(force_rollback=True):      # 사례마다 처음 상태에서
                    sess = AgentSession(onto, AI, task_id=f"llm-{i}")
                    res = run_agent(chat, sess, req, on_step=(lambda it: print(
                        f"  [{it['step']}] {it['tool']}({it['args']}) → {it['result']['status']}")) if detail else None)
                answer = (res["answer"] or res["stopped"] or "").strip()
                tok = f"토큰 입력 {res['usage']['prompt_tokens']:,} 출력 {res['usage']['completion_tokens']:,}"
                if detail:
                    print(f"  답: {answer}")
                    print(f"  단계 {res['steps']}, 도구 호출 {len(res['trace'])}, {tok}")
                else:
                    seq = " ".join(f"{t['tool']}:{t['result']['status']}" for t in res["trace"]) or "(도구 호출 없음)"
                    print(f"  반복 {i + 1}: {seq} | {tok} | {' '.join(answer.split())[:80]}")
        chat.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
