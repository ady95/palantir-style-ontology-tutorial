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
| 19 | Ontology Registry · API · SDK (버전 관리·REST API·타입 SDK 생성) | ✅ v0.1 |
| 20 | 상태 머신 · 상태 전이 강제 · 동시 변경 감지 · 지금 가능한 Action 조회 | ✅ v0.1 |
| 21 | 토큰 인증(JWT·scope) · 행·속성 권한 정책 · 정책 판단 기록 · 승인 기한·미리보기·알림 | ✅ v0.1 |

> 세무 정의(taxos)는 장이 진행되며 버전이 오릅니다. 각 장 원고의 출력은 그 장의 태그 기준입니다.
>
> | 태그 | 원고 | taxos |
> |---|---|---|
> | `part4` | 15~19장 | v0.1.0 |
> | `ch20` | 20장 | v0.2.0 (상태 머신, Action 4개 추가) |
> | `main` | 21장~ | v0.3.0 (보안 정책, 승인 기한) |

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
│   ├── registry.py           # Ontology Registry: 버전 등록·비교(diff)·활성화·롤백             [19장]
│   ├── describe.py           # 공개 메타데이터 (물리 매핑을 숨긴 API·SDK·에이전트용 설명)     [19장]
│   ├── api.py                # FastAPI: 객체·링크·Function·Action·승인 엔드포인트              [19장]
│   ├── sdk.py                # 원격 클라이언트 (httpx 만 사용, 로컬 ObjectSet 과 같은 모양)    [19장]
│   ├── codegen.py            # SDK Generator: 메타데이터 → 타입 있는 Python SDK               [19장]
│   ├── states.py             # 상태 머신: 전이 규칙 조회, Mermaid 다이어그램                   [20장]
│   ├── auth.py               # 인증: JWT 발급·검증(JwtAuth), 개발용 키(ApiKeyAuth), scope        [21장]
│   ├── policy.py             # 권한 정책: 행·속성 정책 집행, 판단 기록                         [21장]
│   ├── loader.py, cli.py
├── examples/17_object_link_engine.py   # PART 2 SPARQL 질문을 엔진으로 다시 풀기
├── examples/17_compare_sparql.py       # SPARQL(RDF 그래프)과 엔진(운영 DB)의 결과 ID 집합 대조
├── examples/18_function_action_engine.py  # Action 실행·거부·승인 시나리오 (기본은 롤백)
├── examples/19_registry_api_sdk.py        # 등록→활성화 관문→API→생성 SDK (기본은 롤백)
├── examples/20_state_transitions.py       # 지금 가능한 Action → 신고 업무 상태 전이 → 동시 변경 감지
├── examples/21_security_hitl.py           # 토큰 인증 → 역할별 조회 → 정책 위반 차단 → 승인 워크플로
├── examples/dev_api_keys.yaml             # 개발용 API 키 (토큰 → 실행자)
├── ontologies/               # 도메인 정의 (엔진 코드 수정 없이 교체)
│   ├── taxos/ontology.yaml       # 세무 v0.3.0: Object 8 · Link 10 · Function 4 · Action 8 · 상태 머신 3 · 정책 2
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

## Ontology Registry · API · SDK (19장)

엔진을 **외부에 여는** 단계입니다. 정의는 Registry 에서 버전으로 관리하고, 활성 버전만 API 로 서빙하며,
API 메타데이터로 타입이 있는 SDK 를 생성합니다.

```mermaid
flowchart LR
  Y[ontology.yaml + functions.py] -->|register: 검증·checksum| D[draft]
  D -->|activate: 사람·다른 사람·호환성·DB 대조| A[active]
  A -->|이전 active| R[retired]
  R -->|activate = 롤백| A
  A --> API[REST API] --> SDK[생성 SDK · AI 에이전트]
```

### Registry: 등록과 반영을 나눈다

```bash
uv run python -m minionto registry register ontologies/taxos/ontology.yaml --actor A001,human
uv run python -m minionto registry activate taxos 0.1.0 --actor A005,human    # 등록자와 다른 사람
uv run python -m minionto registry diff taxos 0.1.0 0.2.0
uv run python -m minionto registry history
```

| 규칙 | 이유 |
|---|---|
| 정의 YAML 과 Function 코드를 함께 저장, checksum 으로 식별 | 활성 버전은 디스크 파일이 바뀌어도 그대로 재현 |
| 같은 버전 번호에 다른 내용 등록 불가 | 등록된 버전은 불변. 바꾸려면 version 을 올린다 |
| 초안 등록은 AI 도 가능, 활성화는 **사람**이 | LLM 이 만든 업무 규칙은 사람의 검토 후에만 실행 환경에 들어간다 |
| 등록자 본인은 draft 를 활성화할 수 없음 | 직무 분리 (18장 승인과 같은 원칙) |
| breaking 변경은 `--allow-breaking` 이 있어야 활성화 | 기존 API·SDK 사용자 보호 |
| 활성화 직전 DB 대조 검사 | 실제 스키마에 없는 컬럼을 가리키는 정의 차단 |

변경은 **사용자에게 미치는 영향**으로 분류합니다 (`diff`, 권장 버전 `suggest_version`).

| 분류 | 예 | 권장 버전 |
|---|---|---|
| breaking | 객체 타입·속성·링크·Action 삭제, 타입 변경, enum 값 삭제, 필수 입력 추가, 이벤트 발행 중단 | 주 버전 |
| policy | 실행 조건·권한·승인 규칙·변경 내용 수정 (입력 형식은 같지만 **업무 규칙**이 바뀜 → 검토) | 부 버전 |
| compatible | 객체·속성·Action 추가, 선택 입력 추가, **테이블·컬럼·조인 키 변경** | 부 버전 |

물리 매핑(테이블·컬럼) 변경이 compatible 인 이유: API 는 업무 용어만 노출하므로 DB 구조를 바꿔도 사용자는 모릅니다.

### API: 업무 용어로 된 엔드포인트

```bash
uv run python -m minionto serve ontologies/taxos/ontology.yaml ontologies/northwind/ontology.yaml     --keys examples/dev_api_keys.yaml --today 2027-01-18          # 개발: 파일을 바로 서빙
uv run python -m minionto serve --registry --keys examples/dev_api_keys.yaml   # 운영: 활성 버전만
# 문서: http://127.0.0.1:8800/docs (OpenAPI)
```

| 메서드·경로 (`/v1/ontologies/{o}` 아래) | 내용 |
|---|---|
| `GET /` | 공개 메타데이터 — 객체·속성·링크·Function·Action(권한·승인·실행 조건). 테이블·컬럼·구현 위치는 없음 |
| `POST /objects/{type}/search` | `where`·`steps`(조건/pivot 순서)·`order_by`·`select`·`limit`·`offset` |
| `POST /objects/{type}/aggregate` | `count_by`, `metrics` |
| `GET /objects/{type}/{pk}`, `…/{pk}/links/{link}` | 객체 하나, 링크 따라가기 |
| `POST /functions/{fn}` | `{"args": {...}}` |
| `GET /actions/{a}/candidates` | **토큰의 실행자**가 실행할 수 있는 대상 |
| `POST /actions/{a}/apply` | `{"target", "params", "dry_run"}` → 200 completed · 202 approval_required · 403 denied · 409 rejected · 422 invalid |
| `GET /approvals`, `POST /approvals/{id}/decide` | 내 역할이 처리할 승인 요청 |

```bash
curl -s -H "Authorization: Bearer dev-ai-agent" -H "Content-Type: application/json"   -d '{"where":[{"field":"status","op":"eq","value":"자료수집중"}],"select":["return_id","due_date"],"limit":2}'   http://127.0.0.1:8800/v1/ontologies/taxos/objects/TaxReturn/search
```

- 실행자(actor)는 **토큰에서** 정합니다. 본문에 `actor` 를 넣어도 무시됩니다. 개발용 키는 21장에서 실제 인증으로 바꿉니다.
- 금액(decimal)은 정밀도를 지키려고 문자열(`"440.00"`)로 보냅니다.

### SDK: 메타데이터 → 타입 있는 클라이언트

```bash
uv run python -m minionto sdk ontologies/taxos/ontology.yaml -o build/taxos_sdk.py
uv run python -m minionto sdk --url http://127.0.0.1:8800 --ontology northwind --token dev-nw-sales -o build/northwind_sdk.py
```

```python
from taxos_sdk import TaxosClient
sdk = TaxosClient("http://127.0.0.1:8800", token="dev-ai-agent")
sdk.check_version()                                         # 주 버전이 다르면 오류 → SDK 재생성
rows = sdk.TaxReturn.where(status="자료수집중").order_by("due_date").limit(5).all()   # list[TaxReturn]
rows[0].due_date                                            # datetime.date
sdk.TaxReturn.where(return_id="R00006").pivot("customer").first()                   # Customer
sdk.request_missing_documents("R00006", note="기한 전 제출 부탁드립니다", dry_run=True)
```

- 객체 타입마다 dataclass (enum → `Literal`, date·decimal 변환). `from_dict` 는 모르는 키를 무시하므로
  서버에 속성이 추가돼도(compatible) 기존 SDK 가 깨지지 않습니다.
- Action 메서드 독스트링에 권한·승인·실행 조건이 들어가 에디터와 AI 코딩 도구가 그대로 읽습니다.
- 생성 SDK 는 `minionto.sdk`(httpx 만 사용)에 의존하며, 타입 없이 쓰려면 `RemoteOntology` 를 바로 씁니다.

```bash
uv run python examples/19_registry_api_sdk.py            # Registry·API·SDK 한 번에 (전부 롤백)
uv run python examples/19_registry_api_sdk.py --commit   # Registry 등록·활성화만 남김
```

## 상태 머신과 상태 전이 (20장)

enum 속성 하나를 상태로 정하고, **어떤 Action 이 어떤 상태를 어디로 바꾸는지** 객체 타입 아래에 선언합니다.
여기 없는 전이는 엔진이 막습니다.

```yaml
  TaxReturn:
    ...
    state_machine:
      property: status
      initial: [자료수집중]
      terminal: [신고완료, 기한후신고]
      transitions:                                   # Action 이름 → 전이
        start_review:         { from: 자료수집중, to: 검토중 }
        return_to_collection: { from: 검토중,     to: 자료수집중 }
        complete_filing:      { from: 검토중,     to: 신고완료 }
        file_late:            { from: 검토중,     to: 기한후신고 }
```

엔진이 하는 일

| 시점 | 동작 |
|---|---|
| 정의 검증 | Action 의 edits 가 상태를 바꾸면 전이가 선언되어 있는지, to 와 값이 같은지, 링크 객체는 where 로 시작 상태를 제한했는지, 새 객체는 initial 상태인지, 종료 상태에서 나가는 전이가 없는지 |
| 실행 전 | 대상의 현재 상태가 from 에 없으면 rejected — 다른 실행 조건의 실패 사유와 **함께** 돌려준다. `candidates()` 도 시작 상태를 조회 조건으로 쓴다 |
| 변경 시 | 바꿀 행을 `FOR UPDATE` 로 잠그고 다시 읽어 **동시 변경을 감지**하고, 링크 객체까지 모든 상태 변경이 선언된 전이인지 다시 확인한다. 어긋나면 전체 롤백 (rejected) |
| 변경 후 | 일어난 전이를 `minionto.state_transitions` 에 기록 (`actions.history(type, pk)`) |

```python
onto.actions.available("TaxReturn", "R00006", actor=AI)      # 지금 할 수 있는 일 (아무것도 바꾸지 않음)
onto.actions.check("return_to_collection", "R00006", actor=TAX)   # → needs_input (reason)
onto.actions.apply("complete_filing", "R00006", actor=TAX, expect={"status": "검토중"})   # 읽은 뒤 바뀌었으면 rejected
onto.actions.history("TaxReturn", "R00006")
```

```bash
uv run python -m minionto states ontologies/taxos/ontology.yaml TaxReturn --mermaid
uv run python -m minionto available ontologies/taxos/ontology.yaml TaxReturn R00006 --actor agent-01,AI비서,agent --today 2027-01-18
uv run python -m minionto run ontologies/taxos/ontology.yaml complete_filing R00024 --actor A001,세무사,human --expect status=검토중
uv run python -m minionto history ontologies/taxos/ontology.yaml TaxReturn R00006
uv run python examples/20_state_transitions.py
```

API: `GET .../objects/{type}/{pk}/actions`, `GET .../objects/{type}/{pk}/history`, apply 요청의 `expect`.
SDK: `sdk.available()`, `sdk.history()`, Action 메서드의 `expect=` 인자, 독스트링의 상태 전이.

## 인증 · 권한 정책 · 승인 워크플로 (21장)

```yaml
security:
  row_policies:                     # 행: 이 실행자에게 보이는 객체 (보이지 않으면 Action 도 불가)
    own_customers:
      description: 실무자는 본인 담당 고객과 그 고객의 신고·자료·업무·상담·위험 이벤트만 다룬다
      roles: [실무자]
      rules:
        Customer:  { field: accountant_id, op: eq, value: $actor.id }
        TaxReturn: { field: customer.accountant_id, op: eq, value: $actor.id }
        ...
  property_policies:                # 속성: 결과에서 빠지고, 조건·정렬·선택에 쓰면 거부
    personal_info:
      description: AI 에이전트는 고객·담당자의 개인 식별 정보를 보지 않는다
      actor_types: [agent]
      hidden: { Customer: [representative, phone, business_reg_no], Accountant: [email] }
  audit_reads: [agent]              # AI 에이전트의 조회를 기록
```

```python
onto.objects("Customer").secure(actor)          # 실행자에게 보이는 범위 (as_actor 는 $actor 값만 정함)
onto.get("Customer", "C0001", actor=actor)      # 보이지 않으면 None
onto.functions.call_as(actor, "recommend_assignee", customer="C0001")   # 입력은 보여야, 출력은 가림
onto.policy.decisions()                         # 정책 판단 기록 (거부, AI 조회)
onto.actions.pending_approvals()                # 기한(expires_at)·변경 미리보기(preview) 포함
onto.actions.expire_stale()                     # 기한 지난 승인 요청 만료 (스케줄러)
```

- 인증: `JwtAuth` 가 서명(HS256)·만료·발급자·대상을 확인하고 토큰의 sub·role·actor_type·scope 로 실행자를 정한다.
  서명 키는 `.env` 의 `MINIONTO_TOKEN_SECRET` (32바이트 이상). scope: `objects:read` `functions:call` `actions:apply` `approvals:decide`
- 승인: 요청할 때 edits 를 실행·롤백해 미리보기를 저장하고, `approval_requested` 이벤트를 outbox 에 남긴다.
  승인 시 기한을 확인하고, 실제 변경이 미리보기와 다르면 실행하지 않고 만료로 닫는다.

```bash
uv run python -m minionto token issue --sub agent-01 --role AI비서 --type agent --scope objects:read,actions:apply --ttl 1h
uv run python -m minionto serve ontologies/taxos/ontology.yaml --auth jwt --today 2027-01-18
uv run python -m minionto query ontologies/taxos/ontology.yaml Customer --as A003,실무자,human
uv run python -m minionto approvals list ontologies/taxos/ontology.yaml
uv run python -m minionto approvals expire ontologies/taxos/ontology.yaml
uv run python -m minionto decisions ontologies/taxos/ontology.yaml
uv run python examples/21_security_hitl.py
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
| 승인 알림은 outbox 이벤트까지만 | 메일·메신저로 실제 발송하지 않음 | 23장 아웃박스 발송기 |
| 상태 전이 외의 변경 이력(before)은 action_log 에만 | 객체별 속성 이력 조회가 불편 | 24장 감사 로그에서 객체 이력 뷰 |
| 상태 머신은 enum 속성 하나, 전이 조건은 Action 의 실행 조건으로 | 병렬 상태(예: 신고 상태 × 납부 상태)는 따로 선언 | 속성마다 별도 객체로 나누거나 이후 확장 |
| SQL 로 DB 를 직접 바꾸면 상태 머신을 거치지 않음 | 엔진 밖 변경은 막지 못함 (다음 Action 에서 동시 변경으로 감지될 뿐) | DB 트리거로 전이 표를 강제하거나 쓰기 권한을 엔진 계정으로 제한 |
| `pk`·`label` 등은 예약 이름 | 같은 이름의 속성은 `obj["pk"]` 로 읽음 | 검증기가 경고 |
| 토큰은 공유 비밀키(HS256)로 직접 발급 | 키가 새면 모든 토큰을 위조할 수 있음, 토큰 폐기 목록 없음 | 사내 인증 서버(OIDC)의 RS256 서명 + JWKS 검증 |
| 행 정책은 결과 타입에만 적용 | 링크 경로 조건(some/none)으로 범위 밖 객체의 존재를 추론할 여지 | 경로마다 정책 적용, 집계 최소 건수 |
| 개수 조회(count)는 조회 기록에 남기지 않음 | AI 가 집계로 얻은 정보는 기록되지 않음 | 집계 기록 추가 |
| API 는 요청마다 DB 연결을 새로 엶 | 동시 요청이 많으면 연결 비용 | 커넥션 풀(psycopg_pool) |
| Registry 는 활성 버전 하나만 서빙 | 버전별 API 동시 제공(`/v1`, `/v2`) 불가 | 서버 재시작으로 전환, 이후 버전 라우팅 |
| Registry 활성화는 서버를 다시 띄워야 반영 | 실행 중 정의 교체 없음 | 활성화 이벤트 구독 후 재적재 |
