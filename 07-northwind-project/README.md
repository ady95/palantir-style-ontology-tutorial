# 07. Northwind 주문·재고 관리 (PART 8, 32장)

세무(taxos)에 쓴 같은 엔진으로 다른 도메인의 업무를 만든다. 재고 부족 감지 → 발주 → 금액별 승인 → 입고.

| 파일 | 내용 |
|---|---|
| [`../03-ontology-engine/ontologies/northwind/ontology.yaml`](../03-ontology-engine/ontologies/northwind/ontology.yaml) | Northwind v0.2.0: 발주(PurchaseOrder), Action 4개(출고·발주·입고·취소), 금액별 승인, 에이전트 연락처 정책 |
| [`../03-ontology-engine/ontologies/northwind/functions.py`](../03-ontology-engine/ontologies/northwind/functions.py) | 재고 부족·제안 수량·금액·재고 계산 Function |
| [`../datasets/northwind/migrations/032_purchasing.sql`](../datasets/northwind/migrations/032_purchasing.sql) | 실습용 발주 테이블 (공개 샘플 DB 에 추가) |
| [`purchasing.py`](purchasing.py) | 발주 업무 그래프 (27장 방식): 감지 → 발주 → 승인 대기 → 보고 |
| [`32_purchasing.py`](32_purchasing.py) | 출고 → 감지·발주 → 승인·재개 → 입고(되쓰기) → 막히는 요청 → (--llm) LLM 에이전트 |

```bash
bash scripts/load_northwind.sh                         # 발주 테이블을 포함해 Northwind 를 다시 적재
uv run python 07-northwind-project/32_purchasing.py    # 모든 변경 롤백
uv run pytest -q tests/test_northwind_purchasing.py
```

| 승인 정책 | 조건 |
|---|---|
| 사람(구매담당·관리자)의 발주 | 금액 1,000 이상이면 관리자 승인 |
| AI 에이전트의 발주 | 금액 300 이상이면 관리자 승인 |

엔진 확장 (32장): Action 의 변경 값에 `{function: 이름, args: {...}}` 를 쓰면 Function 의 결과를 값으로 쓴다
(재고 + 입고 수량 같은 계산). 검증기는 Function 이름·입력·반환 타입을 확인한다.
