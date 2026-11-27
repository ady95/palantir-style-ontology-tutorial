"""18장. Function Engine · Action Engine 실습

기본은 '체험 모드'다. 모든 실행을 바깥 트랜잭션 안에서 하고 마지막에 롤백하므로
몇 번을 실행해도 실습 데이터가 그대로다. 실제로 반영하려면 --commit 을 붙인다.
(되돌리기: bash scripts/load_taxos.sh)

실행: cd 03-ontology-engine && uv run python examples/18_function_action_engine.py [--commit]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minionto import OntologyClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "ontologies"
COMMIT = "--commit" in sys.argv

# 실행자 (합성 데이터의 담당자 ID 사용)
TAX_ACCOUNTANT = {"id": "A001", "role": "세무사", "type": "human"}
STAFF_A003 = {"id": "A003", "role": "실무자", "type": "human"}
AI_AGENT = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def title(text: str) -> None:
    print(f"\n{'-' * 70}\n{text}\n{'-' * 70}")


def functions(onto: OntologyClient) -> None:
    title("1. Function: 정의(YAML)는 계약, 구현(functions.py)은 계산")
    fn = onto.functions
    ret = onto.objects("TaxReturn").where(status="자료수집중", due_date="2027-01-25") \
        .where(**{"customer.risk_level": "높음"}).order_by("return_id").first()
    customer = ret.customer
    print("missing_documents :", [s.doc_type_code for s in fn.call("missing_documents", tax_return=ret)])
    print("filing_delay_risk :", fn.call("filing_delay_risk", customer=customer))
    print("has_capacity(A002):", fn.call("has_capacity", accountant="A002"), " ← 기본키만 넘겨도 객체로 바뀐다")
    print("has_capacity(A004):", fn.call("has_capacity", accountant="A004"))
    best = fn.call("recommend_assignee", customer=customer)
    print("recommend_assignee:", best.object_type, best.pk)


def actions(onto: OntologyClient) -> None:
    eng = onto.actions
    act = "request_missing_documents"

    title("2. 실행 가능한 대상 찾기: 실행 조건을 그대로 조회 조건으로")
    cands = eng.candidates(act, actor=AI_AGENT)
    print(f"AI비서가 '{act}' 를 실행할 수 있는 신고: {cands.count()}건")
    print("고객 위험 등급별:", {r["customer.risk_level"]: r["count"] for r in cands.count_by("customer.risk_level")})
    normal = cands.where({"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first()
    risky = cands.where(**{"customer.risk_level": "높음"}).order_by("return_id").first()

    title("3. 권한: 담당이 아닌 실무자")
    other = STAFF_A003 if normal.customer.accountant_id != "A003" else {**STAFF_A003, "id": "A004"}
    print(eng.apply(act, normal, actor=other))

    title("4. 미리보기(dry-run) → 실행 → 같은 요청 다시")
    print(eng.apply(act, normal, actor=AI_AGENT, params={"note": "기한 전 제출 부탁드립니다"}, dry_run=True))
    print(eng.apply(act, normal, actor=AI_AGENT, params={"note": "기한 전 제출 부탁드립니다"}))
    print(eng.apply(act, normal, actor=AI_AGENT))           # 중복 실행 방지 규칙에 걸린다

    title("5. 승인: 고위험 고객에 대한 AI의 요청")
    pending = eng.apply(act, risky, actor=AI_AGENT)
    print(pending)
    print(eng.decide(pending.approval_id, STAFF_A003))       # 실무자는 승인자가 아니다
    print(eng.decide(pending.approval_id, TAX_ACCOUNTANT, comment="확인함"))

    title("6. Function 을 쓰는 실행 조건: 업무량이 넘친 담당자(A002)의 고객 재배정")
    cust = onto.objects("Customer").where(accountant_id="A002", business_status="영업").order_by("customer_id").first()
    print(eng.apply("assign_accountant", cust, actor=TAX_ACCOUNTANT, params={"accountant": "A003"}))
    best = onto.functions.call("recommend_assignee", customer=cust)
    print("추천 담당자:", best.pk)
    print(eng.apply("assign_accountant", cust, actor=AI_AGENT, params={"accountant": best}))
    print(eng.apply("assign_accountant", cust, actor=TAX_ACCOUNTANT, params={"accountant": best}))

    title("7. 감사 로그와 Outbox (변경과 같은 트랜잭션에 기록됨)")
    for r in onto.conn.execute("""SELECT action, target_pk, actor->>'role' AS role, status
                                  FROM minionto.action_log ORDER BY created_at DESC LIMIT 8""").fetchall()[::-1]:
        print("  log   ", r)
    for r in onto.conn.execute("""SELECT event_type, target_pk FROM minionto.outbox
                                  ORDER BY event_id DESC LIMIT 4""").fetchall()[::-1]:
        print("  outbox", r)


def northwind() -> None:
    title("8. 같은 엔진, 다른 도메인 (Northwind Function)")
    with OntologyClient.open(ROOT / "northwind" / "ontology.yaml") as nw:
        print("order_total(10248):", nw.functions.call("order_total", order=10248))
        low = [p.product_name for p in nw.objects("Product").where(discontinued=0)
               if nw.functions.call("is_low_stock", product=p)]
        print(f"재고 부족 상품 {len(low)}종:", low[:5])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    with OntologyClient.open(ROOT / "taxos" / "ontology.yaml", today="2027-01-18") as onto:
        with onto.conn.transaction(force_rollback=not COMMIT):   # 체험 모드: 끝나면 모두 롤백
            functions(onto)
            actions(onto)
        print("\n※ 체험 모드: 모든 변경을 롤백했습니다 (--commit 으로 실제 반영)" if not COMMIT else "\n※ 반영 완료")
    northwind()
