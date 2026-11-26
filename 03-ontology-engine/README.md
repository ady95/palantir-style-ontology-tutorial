# 03. Mini Ontology Engine — MiniOnto (PART 4)

팔란티어식 온톨로지의 핵심 개념(Object·Link·Interface·Function·Action·Security)을
**YAML 정의(메타데이터)** 로 기술하고, 범용 엔진이 그 정의를 읽어 동작하도록 만듭니다.

> MiniOnto와 그 YAML 문법은 이 책의 자체 설계입니다. 팔란티어 공식 문법이나 내부 구현이 아닙니다.

## 진행 현황

| 장 | 구성 요소 | 상태 |
|---|---|---|
| 16 | 메타모델·YAML DSL·검증기 | ✅ v0.1 |
| 17 | Object Engine · Link Engine (조회·관계 탐색) | 예정 |
| 18 | Function Engine · Action Engine | 예정 |
| 19 | Ontology Registry · API · SDK | 예정 |

## 구조

```text
03-ontology-engine/
├── minionto/                 # 범용 엔진 (도메인 코드 없음)
│   ├── metamodel.py          # DSL 문법 = Pydantic 모델
│   ├── schema.py             # 링크 색인, 경로 해석 (customer.accountant.max_workload)
│   ├── validator.py          # 참조 무결성 검사
│   ├── db.py                 # 실제 PostgreSQL 스키마 대조
│   ├── loader.py, cli.py
├── ontologies/               # 도메인 정의 (엔진 코드 수정 없이 교체)
│   ├── taxos/ontology.yaml       # 세무: Object 8 · Link 10 · Function 4 · Action 4
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

## v0.1의 제약 (의도적으로 단순화)

| 제약 | 영향 | 대안 |
|---|---|---|
| 기본키는 단일 속성만 | Northwind `order_details`(복합키)를 객체로 정의하지 못함 | many_to_many 링크(through)로 표현 |
| 링크에 속성이 없음 | 주문-상품 링크의 수량·단가를 링크에서 읽지 못함 | `order_total` Function에서 계산 |
| 하나의 데이터베이스(datasource) | 여러 DB를 걸친 객체는 정의 불가 | 이후 Data Integration 단계에서 다룸 |
| 조건식은 비교·논리·수량·함수만 | 산술식(예: `재고 + 입고예정 <= 기준`)을 직접 쓰지 못함 | Function으로 분리 (`is_low_stock`) |
