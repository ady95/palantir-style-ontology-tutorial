"""27장. LangGraph 업무 그래프 — 신고 지연 위험 고객 처리 에이전트

    uv run python 03-ontology-engine/examples/27_workflow.py          # 템플릿 문장 (LLM 없이)
    uv run python 03-ontology-engine/examples/27_workflow.py --llm    # 메모·요약을 OpenAI 호환 LLM 으로

1. 그래프 구조   — 노드와 분기 (Mermaid)
2. 실행          — 탐지 → 분석 → 메모 → 실행 … 승인 대기에서 멈춘다
3. 재개          — 재개 값이 아니라 엔진의 승인 기록으로 이어 간다 (새 그래프 객체 = 새 프로세스)
4. 실행 직후 중단 — 다시 실행해도 두 번 실행되지 않는다 (멱등 키)
5. 판단 뒤 변경  — 실행 직전에 대상이 바뀌면 다시 읽고 판단한다
6. 승인 대기 중 변경 — 승인해도 엔진이 만료시키고, 그래프는 만료로 정리한다

체크포인트는 메모리(InMemorySaver)에, 모든 DB 변경은 롤백한다. 체크포인트를 DB 에 두고 프로세스를
나눠 실행하는 것은 CLI(python -m minionto workflow ...)로 한다 (27-3).
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.workflow import REQ, FilingRiskWorkflow, LLMWriter, TemplateWriter, config, interrupts  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def counts(state: dict) -> str:
    return ", ".join(f"{k} {n}" for k, n in Counter(i["status"] for i in state["items"]).items())


def executions(c: OntologyClient, ret: str) -> int:
    return c.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = %s AND target_pk = %s "
                          "AND status = 'completed'", (REQ, ret)).fetchone()["n"]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    writer = TemplateWriter()
    if "--llm" in sys.argv:
        from dotenv import load_dotenv

        from minionto.llm import ChatClient
        load_dotenv(ENGINE.parent / ".env")
        writer = LLMWriter(ChatClient())

    with OntologyClient.open(TAXOS, today=AS_OF) as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()

        section("1. 그래프 구조")
        g = FilingRiskWorkflow(c, AI, writer=writer).build(checkpointer=InMemorySaver())
        print(g.get_graph().draw_mermaid())

        section("2. 실행 — 승인 대기에서 멈춘다")
        with c.conn.transaction(force_rollback=True):
            saver = InMemorySaver()
            g = FilingRiskWorkflow(c, AI, writer=writer).build(checkpointer=saver)
            out = g.invoke({"task_id": "W1", "as_of": AS_OF}, config("W1"))
            print(f"대상 {len(out['items'])}건 → {counts(out)}")
            print(f"체크포인트 {len(list(g.get_state_history(config('W1'))))}개, 다음 노드 {g.get_state(config('W1')).next}")
            for it in out["items"]:
                if it.get("note"):
                    print(f"  {it['return_id']} {it['status']:17s} 메모({it['note_by']}): {it['note']}")
            (wait,) = interrupts(out)
            print(f"멈춘 이유: {wait['message']} → {[w['return_id'] for w in wait['waiting']]}")

            section("3. 재개 — 승인 결과는 엔진의 승인 기록에서 읽는다")
            a, b = wait["waiting"]
            again = g.invoke(Command(resume={"approved": True}), config("W1"))
            print(f"승인 전에 '승인됐다'는 값으로 재개 → 다시 멈춤: {interrupts(again)[0]['message']}")
            print(f"세무사 승인 {a['return_id']}: {c.actions.decide(a['approval_id'], TAX).status}")
            print(f"세무사 반려 {b['return_id']}: "
                  f"{c.actions.decide(b['approval_id'], TAX, approve=False, comment='고객과 통화 후 결정').status}")
            g2 = FilingRiskWorkflow(c, AI, writer=writer).build(checkpointer=saver)     # 새 프로세스라고 치자
            final = g2.invoke(Command(resume="check"), config("W1"))
            rep = final["report"]
            print(f"보고 {rep['counts']}")
            print(f"승인 {rep['approval']}, 확인 실패 {rep['unverified'] or '없음'}")
            for why, ids in rep["handoff_by_reason"].items():
                print(f"  넘김 {len(ids):2d}건  {why}")
            print(f"요약: {final['summary']}")
            if isinstance(writer, LLMWriter):
                print(f"LLM 사용: {writer.usage}")

        section("4. 실행 직후 중단 — 다시 실행해도 한 번만")
        with c.conn.transaction(force_rollback=True):
            crashed: list[str] = []

            def crash_once(item: dict, res: dict) -> None:
                if res["status"] == "completed" and not crashed:
                    crashed.append(item["return_id"])
                    raise RuntimeError("실행 직후 프로세스 중단")
            saver = InMemorySaver()
            try:
                FilingRiskWorkflow(c, AI, on_act=crash_once).build(checkpointer=saver).invoke(
                    {"task_id": "W2", "as_of": AS_OF}, config("W2"))
            except RuntimeError as e:
                print(f"{crashed[0]} 실행 후 중단: {e} — 실행 기록 {executions(c, crashed[0])}건")
            out = FilingRiskWorkflow(c, AI).build(checkpointer=saver).invoke(None, config("W2"))
            it = next(i for i in out["items"] if i["return_id"] == crashed[0])
            print(f"마지막 체크포인트부터 다시 → {it['return_id']} {it['status']} (replayed={it['replayed']}), "
                  f"멱등 키 {it['idempotency_key']}, 실행 기록 {executions(c, crashed[0])}건")

        section("5. 판단 뒤 변경 — 실행 직전에 대상이 바뀌면")
        with c.conn.transaction(force_rollback=True):
            g = FilingRiskWorkflow(c, AI).build(checkpointer=InMemorySaver(), interrupt_before=["act"])
            out = g.invoke({"task_id": "W3", "as_of": AS_OF}, config("W3"))
            ret = out["items"][out["cursor"]]["return_id"]
            c.conn.execute("UPDATE tax_returns SET status = '검토중' WHERE return_id = %s", (ret,))
            print(f"{ret}: 판단(ready) 뒤, 실행 전에 다른 사람이 검토중으로 바꿨다")
            out = g.invoke(None, config("W3"))
            it = next(i for i in out["items"] if i["return_id"] == ret)
            nodes = [x["node"] + (f"({x.get('check') or x.get('status')})" if x["node"] in ("analyze", "act") else "")
                     for x in out["log"] if x.get("return_id") == ret]
            print(f"  경로 {' → '.join(nodes)} → {it['status']}: {it['messages'][0]}")

        section("6. 승인 대기 중 변경 — 승인해도 만료")
        with c.conn.transaction(force_rollback=True):
            saver = InMemorySaver()
            g = FilingRiskWorkflow(c, AI).build(checkpointer=saver)
            out = g.invoke({"task_id": "W4", "as_of": AS_OF}, config("W4"))
            w = interrupts(out)[0]["waiting"][0]
            sub = c.objects("DocumentSubmission").where(return_id=w["return_id"], status="미요청").first()
            r = c.actions.apply("record_document_submission", sub.pk, actor=TAX)
            print(f"승인 대기 중 {w['return_id']} 의 자료 {sub.pk} 접수: {r.status}")
            r = c.actions.decide(w["approval_id"], TAX)
            print(f"세무사가 승인 → 엔진: {r.status} — {r.messages[-1]}")
            out = g.invoke(Command(resume={"stop": True}), config("W4"))
            it = next(i for i in out["items"] if i["return_id"] == w["return_id"])
            print(f"그래프: {it['return_id']} {it['status']} / 보고 {out['report']['counts']}")
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
