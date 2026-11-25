# AI Native Tax OS 합성 데이터 (`taxos`)

세무 사무소 업무를 흉내 낸 **가상 데이터**입니다. 고객명·대표자명·사업자등록번호·연락처는 모두 무작위 조합이며
실제 인물·사업자와 관계가 없습니다.

- 사업자등록번호는 `999-`로 시작하고, 전화번호는 `010-0000-` 형식, 이메일은 `example.com`을 씁니다.
- 생성기: [`scripts/generate_taxos_data.py`](../../scripts/generate_taxos_data.py) (표준 라이브러리만 사용, 시드 고정)
- 스키마: [`schema.sql`](schema.sql)
- 적재: `docker compose up -d` 최초 실행 시 자동, 이미 실행 중이면 `bash scripts/load_taxos.sh`

## 기준 시나리오

| 항목 | 값 |
|---|---|
| 기준일 (`AS_OF`) | 2027-01-18 |
| 현재 신고 | 부가가치세 확정신고 (일반과세자·법인 `2026-2기`, 간이과세자 `2026년`) |
| 신고 기한 | 2027-01-25 (기준일 7일 후) |

> 책의 공통 업무: **신고 기한이 임박한 고객 중 필수 자료를 내지 않은 고객을 찾아 위험도를 분석하고
> 담당자에게 후속 업무를 배정한다.**

## 테이블

| 테이블 | 행 수 | 설명 | 온톨로지 Object 후보 |
|---|---:|---|---|
| `accountants` | 8 | 세무사 2명(승인 권한) + 실무자 6명, 2개 팀 | Accountant |
| `customers` | 200 | 개인 일반 101 · 개인 간이 33 · 법인 66 | Customer |
| `tax_returns` | 565 | 과거 신고 + 현재 신고 | TaxReturn |
| `document_types` | 5 | 필수 자료 유형과 사업자 유형별 적용 여부 | DocumentType |
| `document_submissions` | 800 | 현재 신고의 자료별 제출 상태 | DocumentSubmission |
| `consultations` | 393 | 최근 1년 상담 이력 | Consultation |
| `tasks` | 506 | 후속 업무 (자료요청·신고서검토 등) | Task |
| `risk_events` | 135 | 기한후신고·가산세부과·연락두절·자료지연제출 | RiskEvent |

행 수는 기본 옵션(`--customers 200 --seed 42`) 기준입니다.

## 필수 자료

| 코드 | 자료 | 개인 일반 | 개인 간이 | 법인 |
|---|---|:-:|:-:|:-:|
| D01 | 매출 세금계산서 내역 | O | O | O |
| D02 | 매입 세금계산서 내역 | O | O | O |
| D03 | 신용카드·현금영수증 매출 내역 | O | O | O |
| D04 | 사업용 신용카드 사용내역 | O | | O |
| D05 | 법인 통장 거래내역 | | | O |

실습용으로 단순화한 목록이며 실제 신고 요건과 다를 수 있습니다.

## 상태 값

- `document_submissions.status`: `미요청` → `요청됨` → `제출완료`, 또는 `해당없음`
- `tax_returns.status`: `자료수집중` → `검토중` → `신고완료` (기한 후 신고는 `기한후신고`)
- `tasks.status`: `대기` · `진행중` · `완료` · `취소`
- `tasks.created_by`: `human` · `agent` · `system` — 에이전트가 만든 업무를 구분하기 위한 값

## 일부러 넣은 실습용 사례

| 사례 | 내용 | 다루는 장 |
|---|---|---|
| 자료 미제출 고객 | 현재 신고 중 `미요청`·`요청됨` 자료가 남은 신고 102건 | 공통 업무, Function |
| **자료 현황 미등록 고객** | 영업 중이고 현재 신고는 있지만 `document_submissions` 행이 하나도 없는 고객 6명. "기록이 없다"를 "미제출"로 볼 수 있는가? (Open World Assumption) | PART 2, 7 |
| 휴업 고객 | `status = '휴업'` 10명. 자료가 `미요청`·`해당없음`이며 상담 이력에 휴업 문의가 있음 | 예외 처리 |
| 폐업 고객 | 과거 신고만 있고 현재 신고 없음 | 객체 상태 |
| 업무량 초과 담당자 | 실무자 A002(56명)·A003(43명)이 `max_workload` 40을 넘음 | 담당자 배정 Action |
| 승인이 필요한 고위험 고객 | `risk_level = '높음'` 31명, 과거 기한후신고·가산세 이력이 많음 | 승인 정책, Human-in-the-loop |
| 진행 중인 자료요청 업무 | `요청됨` 자료가 있는 신고에는 이미 `자료요청` 업무가 있음 | 중복 실행 방지(멱등성) |

## 다시 생성하기

```bash
python3 scripts/generate_taxos_data.py                 # 기본값
python3 scripts/generate_taxos_data.py --customers 1000 --seed 7
bash scripts/load_taxos.sh                             # 실행 중인 DB에 다시 적재
```
