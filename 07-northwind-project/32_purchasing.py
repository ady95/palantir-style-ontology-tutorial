"""32장. Northwind 주문·재고 관리 — 재고 부족 감지에서 발주 승인·입고까지

    uv run python 07-northwind-project/32_purchasing.py          # LLM 없이
    uv run python 07-northwind-project/32_purchasing.py --llm    # 6단계: LLM 에이전트(OpenAI 호환 API)

1. 정의        — 세무와 같은 엔진, 다른 도메인 (Northwind v0.2.0)
2. 출고        — 영업담당이 판매 수량을 기록하면 재고가 줄어든다 (Action 의 값 Function)
3. 감지·발주    — 업무 그래프가 부족 상품을 찾아 AI 에이전트 권한으로 발주, 금액별 승인에서 멈춘다
4. 승인·재개    — 관리자가 승인·반려, 그래프는 승인 기록으로 이어 간다
5. 입고        — 구매담당이 입고 처리하면 재고·입고 예정이 바뀐다 (되쓰기), 이벤트와 감사 기록
6. 막히는 요청  — 판매 중단·중복 발주·권한 밖·재고 부족 출고, (--llm) LLM 에이전트

모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(HERE))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402
from minionto.audit import AuditLog  # noqa: E402
from purchasing import PurchasingWorkflow, config  # noqa: E402

NW = ROOT / "03-ontology-engine" / "ontologies" / "northwind" / "ontology.yaml"
SALES = {"id": "E-sales", "role": "영업담당", "type": "human"}
BUYER = {"id": "E-buyer", "role": "구매담당", "type": "human"}
BOSS = {"id": "E-boss", "role": "관리자", "type": "human"}
AI = {"id": "agent-nw", "role": "AI비서", "type": "agent"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def to_sell(c: OntologyClient, n: int = 6) -> list[tuple[int, int]]:
    """아직 부족하지 않은 판매 상품 중 재주문 기준에 가장 가까운 n개 → (상품, 재주문 기준 아래로 내려갈 판매 수량)"""
    rows = [p for p in c.objects("Product").where(discontinued=0).where(units_on_order=0).all()
            if (p.reorder_level or 0) > 0 and (p.units_in_stock or 0) > (p.reorder_level or 0)]
    rows.sort(key=lambda p: (p.units_in_stock - p.reorder_level, p.pk))
    return [(p.pk, p.units_in_stock - p.reorder_level + 1) for p in rows[:n]]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    use_llm = "--llm" in sys.argv
    with OntologyClient.open(NW, today="2026-12-01") as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        o = c.onto
        section("1. 정의 — 같은 엔진, 다른 도메인")
        print(f"{o.ontology.display_name} v{o.ontology.version}: 객체 {len(o.object_types)} · 링크 {len(o.link_types)} · "
              f"Function {len(o.functions)} · Action {len(o.actions)} · 역할 {', '.join(o.roles)}")
        low0 = [p.pk for p in c.objects("Product").all() if c.functions.call("is_low_stock", product=p.pk)]
        print(f"처음 재고 부족 상품: {low0}")

        section("2. 출고 — 판매가 재고를 줄인다")
        for pid, qty in to_sell(c):
            before = c.get("Product", pid).units_in_stock
            r = c.actions.apply("record_sale", pid, actor=SALES, params={"quantity": qty})
            p = c.get("Product", pid)
            print(f"  상품 {pid:2d} {p.product_name[:24]:24s} 재고 {before:3d} → {p.units_in_stock:3d} "
                  f"(재주문 기준 {p.reorder_level}) {r.status}")

        section("3. 감지·발주 — 업무 그래프, AI 에이전트 권한")
        saver = InMemorySaver()
        g = PurchasingWorkflow(c, AI).build(checkpointer=saver)
        out = g.invoke({"task_id": "P1"}, config("P1"))
        print(f"  부족 상품 {len(out['items'])}개 → {dict(Counter(i['status'] for i in out['items']))}")
        for it in out["items"]:
            print(f"  상품 {it['product_id']:2d} {it['name'][:22]:22s} 재고 {it['stock']:3d}+예정 {it['on_order']:3d} "
                  f"≤ 기준 {it['reorder_level']:3d} → 발주 {it['quantity']:3d}개 {float(it['amount']):8.2f} {it['status']}")
        waits = out["__interrupt__"][0].value["waiting"] if out.get("__interrupt__") else []
        print(f"  승인 대기 {len(waits)}건 (AI 는 300 이상 관리자 승인)")

        section("4. 승인·재개")
        for k, w in enumerate(waits):
            ok = k % 2 == 0
            r = c.actions.decide(w["approval_id"], BOSS, approve=ok, comment=None if ok else "이번 달 예산 초과")
            print(f"  관리자 {'승인' if ok else '반려'}: 상품 {w['product_id']} ({float(w['amount']):.2f}) → {r.status}")
        final = PurchasingWorkflow(c, AI).build(checkpointer=saver).invoke(Command(resume="check"), config("P1"))
        print(f"  보고: {final['report']}")

        section("5. 입고 — 운영 데이터에 되쓰기")
        pos = c.objects("PurchaseOrder").where(status="발주완료").order_by("po_id").all()
        for k, po in enumerate(pos[:2]):
            got = po.quantity if k == 0 else po.quantity // 2                # 두 번째는 일부만 들어왔다
            before = c.get("Product", po.product_id)
            r = c.actions.apply("receive_purchase", po.pk, actor=BUYER, params={"received_quantity": got})
            after = c.get("Product", po.product_id)
            print(f"  {po.pk} 상품 {po.product_id} 발주 {po.quantity} · 입고 {got} → 재고 {before.units_in_stock}→"
                  f"{after.units_in_stock}, 입고 예정 {before.units_on_order}→{after.units_on_order} {r.status}")
        trace = AuditLog(c).trace(r.execution_id)
        print(f"  감사: {trace['execution']['action']} 실행자 {trace['execution']['actor']['id']}, 변경 "
              f"{len(trace['changes'])}건, 이벤트 {[e['event_type'] for e in trace['events']]}")
        ev = c.conn.execute("SELECT event_type, count(*) AS n FROM minionto.outbox GROUP BY 1 ORDER BY 1").fetchall()
        print(f"  아웃박스 이벤트: {', '.join(f'{e['event_type']} {e['n']}' for e in ev)}")
        still = [p.pk for p in c.objects("Product").all() if c.functions.call("is_low_stock", product=p.pk)]
        print(f"  지금 재고 부족 상품: {still} (일부 입고·반려·미입고 상품이 남음)")

        section("6. 막히는 요청")
        disc = c.objects("Product").where(discontinued=1).first().pk
        some_po_product = final["items"][0]["product_id"]
        cases = [
            ("판매 중단 상품 발주", lambda: c.actions.apply("request_purchase", disc, actor=BUYER, params={"quantity": 10})),
            ("진행 중 발주가 있는 상품", lambda: c.actions.apply("request_purchase", some_po_product, actor=BUYER,
                                                             params={"quantity": 10})),
            ("영업담당이 발주", lambda: c.actions.apply("request_purchase", low0[0], actor=SALES, params={"quantity": 10})),
            ("재고보다 많이 출고", lambda: c.actions.apply("record_sale", low0[0], actor=SALES, params={"quantity": 9999})),
            ("AI 가 입고 처리", lambda: c.actions.apply("receive_purchase", pos[-1].pk, actor=AI,
                                                     params={"received_quantity": 1})),
        ]
        for name, fn in cases:
            r = fn()
            print(f"  {name:20s} → {r.status}: {' / '.join(r.messages)}")
        s = AgentSession(c, AI)
        hidden = s.context()["object_types"]["Customer"].get("hidden")
        print(f"  AI 에이전트 컨텍스트: 실행할 수 있는 Action {list(s.context()['actions'])}, 고객의 감춘 속성 {hidden}")
        if use_llm:
            from dotenv import load_dotenv

            from minionto.llm import ChatClient, run_agent
            load_dotenv(ROOT / ".env")
            chat = ChatClient()
            for k in range(3):
                with c.conn.transaction(force_rollback=True):
                    s = AgentSession(c, AI, task_id=f"llm{k}")
                    r = run_agent(chat, s, "재고가 부족한 상품을 찾아서, 적당한 수량으로 발주 요청해 줘.", max_steps=15)
                    done = Counter(x["result"].get("status") for x in r["trace"] if x["tool"] == "run_action")
                    answer = (r["answer"] or f"(답 없음: {r['stopped']})")[:120].replace(chr(10), " ")
                    tools = Counter(x["tool"] + (":" + str(x["args"])[:40] if x["tool"] == "call_function" else "")
                                    for x in r["trace"])
                    print(f"  LLM #{k + 1}: 단계 {r['steps']}, 도구 {len(r['trace'])}회, run_action {dict(done)}, 입력 토큰 "
                          f"{r['usage']['prompt_tokens']:,} — {answer}")
                    print(f"     도구: {dict(Counter(x['tool'] for x in r['trace']))}")
                    _ = tools
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
