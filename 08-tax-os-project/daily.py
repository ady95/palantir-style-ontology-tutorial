"""34장. 신고 업무 자동화 — Tax OS 의 하루 (모든 변경 롤백)

    uv run python 08-tax-os-project/daily.py           # LLM 없이 (메모·상담은 템플릿)
    uv run python 08-tax-os-project/daily.py --llm     # 메모·상담 답변을 OpenAI 호환 LLM 으로

1. 위험 분류 동기화   — OWL 추론 → 고객 분류 (30장, 시스템)
2. 상담 응대         — 접수된 문의에 답하거나 넘김 (34-1, AI)
3. 미제출 자료 요청   — 업무 그래프가 위험 고객의 자료를 요청, 승인 대기에서 멈춤 (27장, AI)
4. 업무량 재배정      — 최대치를 넘은 담당자의 고객을 같은 팀의 여유 있는 담당자에게 (세무사)
5. 승인              — 세무사가 승인 대기를 처리하고, 그래프가 이어 간다 (21·27장)
6. 고객 알림         — 아웃박스 발송 작업자가 문자·메신저를 보내고 결과를 객체로 남긴다 (23장, 시스템)
7. 감사 로그로 본 하루 — 누가 무엇을 했나 (24장)
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENGINE = ROOT / "03-ontology-engine"
for p in (ENGINE, ENGINE / "examples", HERE):
    sys.path.insert(0, str(p))

from dotenv import load_dotenv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from consult import Knowledge, LLMResponder, TemplateResponder, handle, open_inquiries  # noqa: E402
from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402
from minionto.audit import AuditLog  # noqa: E402
from minionto.reasoning import infer, sync_segments  # noqa: E402
from minionto.semantic import Mapping  # noqa: E402
from minionto.workflow import FilingRiskWorkflow, LLMWriter, TemplateWriter, config, interrupts  # noqa: E402
from mock_notifier import create_mock_notifier  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
MAPPING = ROOT / "06-hybrid-ontology" / "taxos_mapping.yaml"
AS_OF = "2027-01-18"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}


def integrations():
    import importlib.util
    spec = importlib.util.spec_from_file_location("taxos_integrations", ENGINE / "ontologies" / "taxos" / "integrations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step(n: int, title: str) -> float:
    print(f"\n{'=' * 72}\n{n}. {title}\n{'=' * 72}")
    return time.perf_counter()


def rebalance(c: OntologyClient) -> list[dict]:
    """최대치를 넘은 담당자마다 넘친 수만큼, 위험이 낮은 고객부터 같은 팀의 추천 담당자에게 (세무사가 실행)"""
    out = []
    for acc in c.objects("Accountant").where(role="실무자").order_by("accountant_id").all():
        mine = c.objects("Customer").where(accountant_id=acc.pk)
        excess = mine.count() - acc.max_workload
        if excess <= 0:
            continue
        rank = {"낮음": 0, "보통": 1, "높음": 2}
        custs = sorted(mine.all(), key=lambda x: (rank[x.risk_level], x.pk))
        moved = 0
        for cu in custs:
            if moved >= excess:
                break
            rec = c.functions.call("recommend_assignee", customer=cu.pk)
            if rec is None:
                out.append({"from": acc.pk, "customer": cu.pk, "status": "추천 없음"})
                break
            r = c.actions.apply("assign_accountant", cu.pk, actor=TAX, params={"accountant": rec.pk})
            out.append({"from": acc.pk, "to": rec.pk, "customer": cu.pk, "status": r.status,
                        "why": (r.messages or [""])[0]})
            moved += r.ok
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")
    use_llm = "--llm" in sys.argv
    chat = None
    if use_llm:
        from minionto.llm import ChatClient
        chat = ChatClient()
    with OntologyClient.open(TAXOS, today=AS_OF) as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        start_log = c.conn.execute("SELECT coalesce(max(created_at), '1900-01-01') AS t FROM minionto.action_log").fetchone()["t"]
        start_out = c.conn.execute("SELECT coalesce(max(event_id), 0) AS n FROM minionto.outbox").fetchone()["n"]

        t = step(1, "위험 분류 동기화 (30장, 시스템)")
        inf = infer(c, Mapping.load(MAPPING))
        res = sync_segments(c, Mapping.load(MAPPING), inf)
        print(f"  추론 {inf.seconds}초 · 분류 반영 {dict(Counter(r['status'] for r in res))} · {time.perf_counter() - t:.1f}초")

        t = step(2, "상담 응대 (34-1, AI)")
        kb = Knowledge()
        responder = LLMResponder(chat) if chat else TemplateResponder()
        s = AgentSession(c, AI, task_id="daily-consult", max_calls=10_000, max_rows=100)
        results = [handle(s, kb, responder, q) for q in open_inquiries(c)]
        print(f"  문의 {len(results)}건 → 답변 {sum(r.route == 'answered' for r in results)}, 넘김 "
              f"{sum(r.route == 'escalated' for r in results)} {dict(Counter(r.by for r in results if r.route == 'escalated'))} "
              f"({responder.name}) · {time.perf_counter() - t:.1f}초")

        t = step(3, "미제출 자료 요청 (27장 업무 그래프, AI)")
        saver = InMemorySaver()
        writer = LLMWriter(chat) if chat else TemplateWriter()
        out = FilingRiskWorkflow(c, AI, writer=writer).build(checkpointer=saver).invoke(
            {"task_id": "daily", "as_of": AS_OF}, config("daily"))
        st = Counter(i["status"] for i in out["items"])
        waits = interrupts(out)[0]["waiting"] if interrupts(out) else []
        print(f"  대상 {len(out['items'])}건 → {dict(st)} · 승인 대기 {len(waits)}건 · {time.perf_counter() - t:.1f}초")

        t = step(4, "업무량 재배정 (세무사)")
        before = {a.pk: c.objects("Customer").where(accountant_id=a.pk).count()
                  for a in c.objects("Accountant").all()}
        moves = rebalance(c)
        after = {a.pk: c.objects("Customer").where(accountant_id=a.pk).count() for a in c.objects("Accountant").all()}
        over = {k: f"{before[k]}→{after[k]}" for k in before if before[k] != after[k]}
        print(f"  재배정 {dict(Counter(m['status'] for m in moves))} · 담당 고객 수 변화 {over}")
        for m in [m for m in moves if m["status"] != "completed"][:3]:
            print(f"    {m}")
        print(f"  {time.perf_counter() - t:.1f}초")

        t = step(5, "승인 (세무사) → 그래프 재개")
        for w in waits:
            r = c.actions.decide(w["approval_id"], TAX)
            print(f"  승인 {w['return_id']} → {r.status}")
        final = FilingRiskWorkflow(c, AI, writer=writer).build(checkpointer=saver).invoke(Command(resume="check"), config("daily"))
        print(f"  보고 {final['report']['counts']} · 확인 실패 {final['report']['unverified'] or '없음'}")

        t = step(6, "고객 알림 (23장 아웃박스 발송, 시스템)")
        app = create_mock_notifier()
        it = integrations()
        d = it.make_dispatcher(c, it.Notifier(TestClient(app)))
        outcomes = []
        for _ in range(10):
            got = d.run_once()
            if not got:
                break
            outcomes += got
        sent = app.state.box["messages"]
        kinds = {r["event_type"]: r["n"] for r in c.conn.execute(
            "SELECT event_type, count(*) AS n FROM notifications GROUP BY 1 ORDER BY 1").fetchall()}
        print(f"  발송 처리 {len(outcomes)}건 {kinds} · 모의 발송 서비스가 받은 메시지 {len(sent)}건")
        sample = next((m for m in sent.values() if m.get("channel") == "문자"), None)
        if sample:
            print(f"  예: {sample['text'][:90]}")
        n = c.conn.execute("SELECT count(*) AS n FROM notifications").fetchone()["n"]
        print(f"  알림 발송 결과(Notification) {n}건 · {time.perf_counter() - t:.1f}초")

        step(7, "감사 로그로 본 하루 (24장)")
        rows = c.conn.execute("""SELECT actor->>'type' AS t, action, status, count(*) AS n FROM minionto.action_log
                                 WHERE created_at > %s GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", (start_log,)).fetchall()
        for r in rows:
            print(f"  {r['t']:6s} {r['action']:28s} {r['status']:18s} {r['n']:3d}")
        ev = c.conn.execute("SELECT event_type, count(*) AS n FROM minionto.outbox WHERE event_id > %s "
                            "GROUP BY 1 ORDER BY 2 DESC", (start_out,)).fetchall()
        print("  이벤트: " + ", ".join(f"{e['event_type']} {e['n']}" for e in ev))
        done = next(i for i in final["items"] if i["status"] == "completed")
        tr = AuditLog(c).trace(done["execution_id"])
        chain = [f"{tr['execution']['action']}({tr['execution']['actor']['id']})"]
        for e in tr["events"]:
            chain.append(f"{e['event_type']}[{e['status']}]")
            for nt in e["notifications"]:
                chain.append(f"Notification {nt['notification_id']} {nt['status']}")
        print(f"  {done['return_id']} 추적: {' → '.join(chain)}")
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
