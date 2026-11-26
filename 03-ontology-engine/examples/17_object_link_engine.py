"""17장. Object Engine · Link Engine 실습

PART 2에서 SPARQL로 구했던 질문을, 이번에는 운영 DB 위의 온톨로지 이름만으로 다시 묻는다.
결과가 같다면 '의미 계층'이 저장 방식(RDF ↔ RDB)과 무관하다는 뜻이다.

실행: cd 03-ontology-engine && uv run python examples/17_object_link_engine.py [--sql]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minionto import OntologyClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "ontologies"
AS_OF, CURRENT_DUE = "2027-01-18", "2027-01-25"     # PART 2 와 같은 기준일
SHOW_SQL = "--sql" in sys.argv


def title(text: str) -> None:
    print(f"\n{'-' * 70}\n{text}\n{'-' * 70}")


def show_sql(q) -> None:
    if SHOW_SQL:
        print(q.sql() + "\n")


def taxos() -> None:
    with OntologyClient.open(ROOT / "taxos" / "ontology.yaml", today=AS_OF) as onto:
        # ------------------------------------------------------------ 1. 조건 + 링크 수량(some)
        title("1. 기한 임박 + 자료 미제출 고객  (PART 2 Q2와 같은 질문)")
        current_open = {"all": [
            {"field": "status", "op": "eq", "value": "자료수집중"},
            {"field": "due_date", "op": "gte", "value": "$today"},
            {"field": "due_date", "op": "lte", "value": CURRENT_DUE},
            {"some": {"link": "submissions",
                      "where": {"field": "status", "op": "in", "value": ["미요청", "요청됨"]}}},
        ]}
        customers = (onto.objects("Customer")
                     .where(business_status="영업")
                     .where({"some": {"link": "tax_returns", "where": current_open}}))
        show_sql(customers)
        print(f"대상 고객: {customers.count()}명")
        print("위험 등급별:", {r["risk_level"]: r["count"] for r in customers.count_by("risk_level")})
        for row in customers.order_by("-risk_level").limit(5).rows(
                "customer_id", "risk_level", "accountant.name"):
            print("  ", row)

        # ------------------------------------------------------------ 2. none: 기록이 없는 객체
        title("2. 자료 현황이 하나도 등록되지 않은 현재 신고  (PART 2 Q3, Open World 문제)")
        unregistered = (onto.objects("TaxReturn")
                        .where(due_date=CURRENT_DUE)
                        .where({"none": {"link": "submissions"}}))
        show_sql(unregistered)
        print(f"미등록 신고: {unregistered.count()}건 → ", [r.pk for r in unregistered])

        # ------------------------------------------------------------ 3. 링크 경로로 그룹 집계
        title("3. 담당 고객 수가 한도를 넘은 담당자  (PART 2 Q4)")
        by_acc = onto.objects("Customer").count_by("accountant.accountant_id", "accountant.max_workload")
        over = [r for r in by_acc if r["count"] > r["accountant.max_workload"]]
        for r in over:
            print(f"  {r['accountant.accountant_id']}: {r['count']}명 / 한도 {r['accountant.max_workload']}")
        print("  ※ '개수 > 속성' 비교는 조건식으로 쓰지 않고 집계 후 판단했다 → 18장 has_capacity Function")

        # ------------------------------------------------------------ 4. 객체 탐색 (Link Engine)
        title("4. 객체 하나에서 링크 따라가기")
        ret = (onto.objects("TaxReturn").where(current_open)
               .where({"field": "customer.risk_level", "op": "eq", "value": "높음"})
               .order_by("due_date").first())
        print("신고        :", ret, ret.tax_kind, ret.due_date)
        print("고객        :", ret.customer, "/ 위험", ret.customer.risk_level)
        print("담당자      :", ret.customer.accountant, "/ 역할", ret.customer.accountant.role)
        missing = ret.submissions.where(status=["미요청", "요청됨"])
        print("미제출 자료 :", [(s.document_type.doc_name, s.status) for s in missing])
        print("진행 중 업무:", ret.tasks.where(status=["대기", "진행중"]).count(), "건")

        # ------------------------------------------------------------ 5. pivot (Search Around)
        title("5. 집합 단위 이동: 담당자 A002 → 담당 고객 → 현재 진행 중 신고")
        a002_returns = (onto.objects("Accountant").where(accountant_id="A002")
                        .pivot("customers")
                        .pivot("tax_returns").where(current_open))
        show_sql(a002_returns)
        print(f"A002 담당 고객의 자료 미비 신고: {a002_returns.count()}건")
        print("고객 영업 상태별:", a002_returns.count_by("customer.business_status"))


def northwind() -> None:
    with OntologyClient.open(ROOT / "northwind" / "ontology.yaml") as nw:
        title("6. 같은 엔진, 다른 도메인 (Northwind)")
        german = nw.objects("Customer").where(country="Germany")
        products = german.pivot("orders").pivot("products")          # 다대다 링크 pivot
        show_sql(products)
        print(f"독일 고객이 주문한 상품: {products.count()}종")
        print("분류별:", [(r["category.category_name"], r["count"]) for r in products.count_by("category.category_name")][:4])

        fuller = nw.objects("Employee").where(last_name="Fuller")
        print("Fuller 의 직속 부하:", [e.title for e in fuller.pivot("direct_reports")])     # 자기 참조 링크
        late = nw.objects("Order").where({"field": "shipped_date", "op": "gt", "value": "$target.required_date"})
        print(f"납기보다 늦게 출고된 주문: {late.count()}건,",
              "운송비 합계", late.aggregate(total=("sum", "freight"))["total"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    taxos()
    northwind()
