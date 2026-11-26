# 03. Mini Ontology Engine — MiniOnto (PART 4)

팔란티어식 온톨로지의 핵심 개념(Object·Link·Interface·Function·Action·Security)을
**YAML 정의(메타데이터)** 로 기술하고, 범용 엔진이 그 정의를 읽어 동작하도록 만듭니다.

> MiniOnto와 그 YAML 문법은 이 책의 자체 설계입니다. 팔란티어 공식 문법이나 내부 구현이 아닙니다.

## 진행 현황

| 장 | 구성 요소 | 상태 |
|---|---|---|
| 16 | 메타모델·YAML DSL·검증기 | ✅ v0.1 |
| 17 | Object Engine · Link Engine (조회·관계 탐색) | ✅ v0.1 |
| 18 | Function Engine · Action Engine (계산·검증된 실행·승인·감사) | ✅ v0.1 |
| 19 | Ontology Registry · API · SDK | 예정 |

## 구조

```text
03-ontology-engine/
├── minionto/                 # 범용 엔진 (도메인 코드 없음)
│   ├── metamodel.py          # DSL 문법 = Pydantic 모델
│   ├── schema.py             # 링크 색인, 경로 해석 (customer.accountant.max_workload)
│   ├── validator.py          # 참조 무결성 검사
│   ├── db.py                 # 실제 PostgreSQL 스키마 대조
│   ├── query.py              # 조건식 → SQL 컴파일러 (경로 → JOIN, some/none → EXISTS)   [17장]
│   ├── objects.py            # OntologyClient · ObjectSet · OntoObject                    [17장]
│   ├── functions.py          # Function Engine: 구현 로드, 입력 변환, 출력 계약 검사      [18장]
│   ├── actions.py            # Action Engine: 권한→조건→승인→변경→Outbox·감사 (한 트랜잭션) [18장]
│   ├── loader.py, cli.py
├── examples/17_object_link_engine.py   # PART 2 SPARQL 질문을 엔진으로 다시 풀기
├── examples/18_function_action_engine.py  # Action 실행·거부·승인 시나리오 (기본은 롤백)
├── ontologies/               # 도메인 정의 (엔진 코드 수정 없이 교체)
│   ├── taxos/ontology.yaml       # 세무: Object 8 · Link 10 · Function 4 · Action 4
│   ├── taxos/functions.py        # Function 구현 (계산만, 테이블 이름 없음)
│   └── northwind/ontology.yaml   # 주문·재고: Object 7 · Link 7 · Function 2
└── schema/minionto.schema.json   # 에디터 자동완성용 JSON Schema
```

## 사용법

```bash
cd 03-ontology-engine
uv run python -m minionto validate ontologies/taxos/ontology.yaml --db   # 문법 → 참조 → DB 대조
uv run python -m minionto describe ontologies/taxos/ontology.yaml --db   # 객체·링크·Action 요약
uv run python -m minionto schema -o schema/minionto.schema.json          # JSON Schema 생성
```

`--db`는 저장소 루트 `.env`의 `TAXOS_DATABASE_URL`·`NORTHWIND_DATABASE_URL`을 씁니다.
VS Code의 YAML 확장을 쓰면 정의 파일 첫 줄의 `$schema` 주석으로 자동완성과 오류 표시가 됩니다.

## 3단계 검증

| 단계 | 검사 내용 | 예 |
|---|---|---|
| 1. 문법 (`metamodel.py`) | 키 오타, 필수 항목, 형식 조합 | `cardinalty` 오타, enum에 values 없음, 조건식에 형식 두 개 |
| 2. 참조 (`validator.py`) | 가리키는 대상이 실제로 있고 뜻이 통하는가 | 없는 객체 타입·속성·링크·파라미터·역할, 허용되지 않은 enum 값, 여러 개로 갈라지는 경로를 단일 비교에 사용, 새 객체의 필수 속성 누락, 기본키 수정 |
| 3. DB (`db.py`) | 정의와 운영 DB가 맞는가 | 없는 테이블·컬럼, 타입 불일치, PRIMARY KEY 제약 없음, 매핑하지 않은 컬럼(경고) |

---

## DSL 요약 (v0.1)

### Object Type · Property

```yaml
object_types:
  Customer:
    display_name: 고객
    source: { table: customers, primary_key: customer_id }   # primary_key 는 '속성' 이름
    title_property: company_name
    implements: [Contactable]
    properties:
      business_status: { type: enum, values: [영업, 휴업, 폐업], column: status, required: true }
```

- 속성 타입: `string` `integer` `decimal` `float` `boolean` `date` `datetime` `enum` `binary`
- `column`을 생략하면 속성명과 같은 컬럼. 속성명을 업무 용어로 바꿀 수 있다(`status` → `business_status`).
- `source.id_strategy`: `provided`(기본) · `uuid` · `prefixed_sequence`(+`id_prefix`) — Action이 새 객체를 만들 때 ID 생성 방식

### Interface

여러 객체 타입이 공유하는 속성 형태. `implements`한 객체 타입은 해당 속성을 같은 타입으로 가져야 한다.

### Link Type

```yaml
link_types:
  customer_tax_returns:
    { from: Customer, to: TaxReturn, cardinality: one_to_many,
      from_key: customer_id, to_key: customer_id, forward_name: tax_returns, reverse_name: customer }
  order_products:                          # 다대다는 연결 테이블(through)로
    from: Order
    to: Product
    cardinality: many_to_many
    through: { table: order_details, from_column: order_id, to_column: product_id }
    forward_name: products
    reverse_name: orders
```

`forward_name`은 from 쪽에서, `reverse_name`은 to 쪽에서 부르는 이름이다. 이 이름으로 경로를 만든다:
`TaxReturn` 기준 `customer.accountant.max_workload`.

### Function

```yaml
functions:
  has_capacity:
    description: 담당 고객 수가 최대치 미만인가
    inputs:  { accountant: { type: object, object_type: Accountant } }
    returns: { type: boolean }
    implementation: functions:has_capacity      # ontologies/taxos/functions.py 의 함수 (18장)
```

파라미터 타입: 속성 타입 + `object`(object_type) · `list`(items) · `struct`(fields)

### 조건식

Action의 실행 조건·권한·승인 조건에 쓴다. 아래 형식 중 **정확히 하나**를 쓴다.

| 형식 | 예 |
|---|---|
| 비교 | `{ field: customer.risk_level, op: eq, value: 높음 }` |
| 논리 | `{ all: [...] }` `{ any: [...] }` `{ not: {...} }` |
| 링크 수량 | `{ some: { link: submissions, where: {...} } }` `{ none: {...} }` |
| 함수 | `{ function: has_capacity, args: { accountant: $params.accountant } }` |

- 연산자: `eq` `ne` `in` `not_in` `gt` `gte` `lt` `lte` `is_null` `not_null`
- `field`는 Action 대상 객체 기준 경로이거나 `$actor.*`·`$params.*`
- `$`로 시작하는 값은 참조: `$actor.id|role|type`, `$params.<이름>`, `$target.<경로>`, `$now`, `$today`
- 여러 객체로 갈라지는 경로(`submissions.status`)는 단일 비교에 쓸 수 없다 → `some`/`none` 사용

### Action

```yaml
actions:
  request_missing_documents:
    display_name: 누락 자료 요청
    target: TaxReturn
    parameters: { note: { type: string, required: false } }
    preconditions:                       # 업무 규칙: 실패 시 message 를 돌려준다
      - message: 자료수집중인 신고만 자료를 요청할 수 있습니다.
        check: { field: status, op: eq, value: 자료수집중 }
    permission:                          # 누가 실행할 수 있는가
      roles: [세무사, 실무자, AI비서]
      actor_types: [human, agent]
      check: { any: [ ... ] }
    approval:                            # 언제 사람의 승인이 필요한가
      required_if: { all: [ { field: customer.risk_level, op: eq, value: 높음 },
                            { field: $actor.type, op: eq, value: agent } ] }
      approver_roles: [세무사]
    edits:                               # 무엇을 바꾸는가 (한 트랜잭션)
      - update_linked: submissions
        where: { field: status, op: eq, value: 미요청 }
        set: { status: 요청됨, requested_at: $today }
      - create: Task
        values: { customer_id: $target.customer_id, task_type: 자료요청, created_by: $actor.type, ... }
    emits: [documents_requested]         # 실행 후 발행할 이벤트 (Outbox, 23장)
```

edit 형식: `{update: target, set}` · `{update_linked: <링크>, where, set}` · `{create: <ObjectType>, values}`

Action은 **코드가 아니라 선언**이다. 무엇을 확인하고(preconditions), 누가 하며(permission),
언제 사람이 개입하고(approval), 무엇을 바꾸는지(edits)를 한곳에 적는다.
사람·웹 화면·AI 에이전트가 모두 이 정의를 거쳐 실행하므로, 규칙을 도구마다 따로 구현하지 않아도 된다.

---

## Object Engine · Link Engine (17장)

테이블·컬럼·조인 키를 몰라도 **객체·속성·링크 이름**으로 조회한다.

```python
from minionto import OntologyClient

onto = OntologyClient.open("ontologies/taxos/ontology.yaml", today="2027-01-18")

# 집합 정의 — 실행 전까지 DB를 읽지 않는다 (where 는 AND 로 쌓인다)
returns = (onto.objects("TaxReturn")
           .where(status="자료수집중")                                   # 속성=값
           .where({"field": "customer.risk_level", "op": "eq", "value": "높음"})   # 링크 경로
           .where({"some": {"link": "submissions",
                            "where": {"field": "status", "op": "eq", "value": "미요청"}}})
           .order_by("due_date", "-customer.risk_level"))

print(returns.sql())                                # 생성된 SQL
returns.count(); returns.first(); returns.limit(10).all()
returns.rows("return_id", "customer.company_name", "customer.accountant.name")   # 표 조회
returns.count_by("customer.accountant.name")        # 그룹별 개수
onto.objects("Customer").pivot("tax_returns")        # 집합 단위 링크 이동 (Search Around)

r = returns.first()
r.customer.accountant.name                           # 단일 값 링크 → 객체
r.submissions.where(status="미요청").all()          # 여러 값 링크 → ObjectSet
```

```bash
uv run python -m minionto query ontologies/taxos/ontology.yaml Accountant \
    -w accountant_id=A002 -p customers -w risk_level=높음 --select customer_id,risk_level -v
uv run python examples/17_object_link_engine.py --sql
```

### 번역 규칙

| 온톨로지 표현 | SQL |
|---|---|
| 속성 `status` | `t0.status` (속성명 ≠ 컬럼명이면 매핑: `business_status` → `status`) |
| 단일 값 경로 `customer.accountant.name` | `LEFT JOIN` (같은 경로는 한 번만 조인) |
| `some` / `none` | 상관 `EXISTS` / `NOT EXISTS` |
| 다대다 링크 | 연결 테이블(through)을 거치는 `EXISTS` |
| `pivot(link)` | 도착 타입 기준 `EXISTS(역방향 링크 → 원래 조건)` |
| `$target.<경로>` | 대상 객체의 다른 속성과 비교 (`shipped_date > $target.required_date`) |
| enum 정렬 | 정의한 값 순서 (`낮음 < 보통 < 높음`), 가나다순 아님 |

- 여러 값 링크를 비교·정렬에 쓰면 오류: 결과 행이 중복되거나 뜻이 모호해지기 때문 → `some`/`none`/`pivot`
- `ne` 는 `IS DISTINCT FROM`, `not` 은 `NOT COALESCE(…, FALSE)` — NULL 이 섞여도 '아니다'가 참
- 값은 `sql.Literal` 로 넣어 SQL 인젝션이 생기지 않고, enum 은 허용 값인지 먼저 확인한다
- Function 조건은 SQL로 바꾸지 않는다 (18장 Function Engine이 Python에서 평가)

### PART 2(SPARQL)와 결과 대조

| 질문 | SPARQL (PART 2) | MiniOnto (17장) |
|---|---|---|
| 기한 임박 + 자료 미제출 고객 | 92명 | 92명 |
| 자료 현황 미등록 현재 신고 | 6건 | 6건 |
| 업무량 한도 초과 담당자 | A002, A003 | A002, A003 |

같은 의미 모델을 RDF 그래프에서 묻든 운영 DB에서 묻든 답이 같다.

---

## Function Engine · Action Engine (18장)

### Function: 계약은 YAML, 계산은 Python

```python
# ontologies/taxos/functions.py — 첫 인자 ctx 로 온톨로지 조회·기준일을 받는다
def has_capacity(ctx, accountant):
    return accountant.customers.count() < accountant.max_workload
```

```python
onto.functions.call("has_capacity", accountant="A002")          # 기본키만 넘겨도 객체로 변환 → False
onto.functions.call("filing_delay_risk", customer="C0008")       # {"score": 82, "level": "높음", "reasons": [...]}
```

엔진은 정의에 없는 입력·빠진 필수 입력·없는 객체를 막고, **반환값이 선언한 타입과 다르면 오류**를 낸다
(`boolean` 이라 했는데 `None` 을 돌려주는 구현). 링크 속성처럼 온톨로지로 표현하지 못하는 계산은
`ctx.sql()` 탈출구를 쓴다 (`order_total`).

### Action: 선언된 업무를 검증하고 한 트랜잭션으로 실행

```python
eng = onto.actions
eng.candidates("request_missing_documents", actor=ai)       # 실행 가능한 대상 (실행 조건 = 조회 조건)
r = eng.apply("request_missing_documents", "R00006", actor=ai, params={"note": "..."}, dry_run=True)
r.status, r.messages, r.changes                                # 미리보기 → 롤백
eng.apply(...)                                                 # 실제 실행
eng.decide(approval_id, approver, approve=True)                # 승인 → 요청자 권한으로 재검사 후 실행
```

| 단계 | 내용 | 실패 시 `status` |
|---|---|---|
| 역할 | `permission.roles`, `actor_types` (입력 검사보다 먼저 — 권한 없는 실행자에게 정보를 주지 않음) | `denied` |
| 입력 | 파라미터 타입·필수·enum, object 파라미터의 객체 존재 | `invalid` |
| 대상 잠금 | `SELECT … FOR UPDATE` — 같은 대상의 동시 실행을 줄 세움 | `invalid` (없음) |
| 권한 조건 | `permission.check` (본인 담당 고객만 등) | `denied` |
| 실행 조건 | `preconditions` — 실패한 규칙의 message 를 **모두** 돌려줌 | `rejected` |
| 승인 | `approval.required_if` 가 참이면 실행하지 않고 승인 요청 저장 | `approval_required` |
| 변경 | `edits` 순서대로. `prefixed_sequence` ID 는 advisory lock 으로 채번 | DB 오류 → `failed` (전체 롤백) |
| 이벤트·감사 | `minionto.outbox`, `minionto.action_log` — 변경과 **같은 트랜잭션** | |

- 조건식에 Function 이 있으면 `all/any/not` 을 Python 에서 나눠 평가하고, 나머지는 SQL 한 번으로 평가한다.
- 승인자는 `approver_roles` 의 **사람**이어야 하고 요청자 본인은 승인할 수 없다(직무 분리).
  승인 시점에 실행 조건을 다시 검사하므로, 그사이 다른 사람이 처리했으면 요청은 `expired` 가 된다.
- 엔진 테이블은 도메인 DB의 `minionto` 스키마에 자동 생성된다. 업무 데이터와 같은 DB에 두어야
  변경·이벤트·감사 기록을 하나의 트랜잭션으로 묶을 수 있다(Transactional Outbox, 23장).

```bash
uv run python examples/18_function_action_engine.py            # 체험 모드: 끝나면 전부 롤백
uv run python examples/18_function_action_engine.py --commit   # 실제 반영 (되돌리기: bash scripts/load_taxos.sh)
```

## v0.1의 제약 (의도적으로 단순화)

| 제약 | 영향 | 대안 |
|---|---|---|
| 기본키는 단일 속성만 | Northwind `order_details`(복합키)를 객체로 정의하지 못함 | many_to_many 링크(through)로 표현 |
| 링크에 속성이 없음 | 주문-상품 링크의 수량·단가를 링크에서 읽지 못함 | `order_total` Function에서 계산 |
| 하나의 데이터베이스(datasource) | 여러 DB를 걸친 객체는 정의 불가 | 이후 Data Integration 단계에서 다룸 |
| 조건식은 비교·논리·수량·함수만 | 산술식(예: `재고 + 입고예정 <= 기준`)을 직접 쓰지 못함 | Function으로 분리 (`is_low_stock`) |
| 링크 개수와 속성 비교 불가 | "담당 고객 수 > max_workload" 를 조건식으로 못 씀 | `count_by` 후 판단, 또는 Function (`has_capacity`) |
| 객체 링크 접근은 매번 질의 | `for r in returns: r.customer` 는 N+1 질의 | 표가 필요하면 `rows("customer.company_name")` 로 한 번에 |
| 승인 요청에 만료 시간이 없음 | 오래된 요청이 남음 | 21장 승인 워크플로에서 기한·알림 추가 |
| 변경 이력(before)은 action_log 에만 | 객체별 이력 조회가 불편 | 22장 감사 로그에서 객체 이력 뷰 |
| `pk`·`label` 등은 예약 이름 | 같은 이름의 속성은 `obj["pk"]` 로 읽음 | 검증기가 경고 |
