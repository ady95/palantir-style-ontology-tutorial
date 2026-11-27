# palantir-style-ontology-tutorial

위키독스 도서 **「팔란티어식 온톨로지 따라하기」** 의 예제 코드 저장소입니다.

> 전통적인 온톨로지에서 AI 에이전트까지, 오픈소스로 구현하는 기업용 지식·의사결정·업무 실행 시스템

- 위키독스: https://wikidocs.net/book/22337

## 소개

팔란티어 제품 사용법이 아니라, 팔란티어식 온톨로지의 설계 사상(Object·Link·Function·Action·Security·Write-back)을
PostgreSQL + Python 기반 오픈소스로 직접 구현하고 AI 에이전트와 결합하는 실습 코드를 제공합니다.

> 이 저장소는 팔란티어 제품의 내부 구현을 복제하거나 묘사하지 않습니다. 핵심 설계 개념을 재현하는 교육·연구용 PoC입니다.

## 저장소 구성 (예정)

| 디렉터리 | 내용 |
|---|---|
| `01-traditional-ontology/` | RDF·OWL·SPARQL·SHACL 실습 (PART 2) |
| `02-operational-ontology/` | Object·Link·Action 실습 |
| `03-ontology-engine/` | 범용 Mini Ontology Engine (MiniOnto) — 메타모델·YAML DSL·검증기·Object/Link·Function/Action Engine·Registry/API/SDK·상태 머신·인증·권한 정책 |
| `04-security-governance/` | 권한·승인·감사 |
| `05-agent-integration/` | MCP·LangGraph 연동 |
| `06-hybrid-ontology/` | Semantic + Operational 하이브리드 (29장 매핑부터) |
| `07-northwind-project/` | Northwind 기반 주문·재고 관리 (32장 발주 업무) |
| `08-tax-os-project/` | 세무 업무 자동화 (합성 데이터) |
| `09-auto-ontology/` | Operational Ontology 자동 생성 |
| `10-benchmarks/` | PoC·비교 실험 (28장 `agent_comparison/` 부터) |
| `datasets/` | 공개·합성 실습 데이터 |
| `tests/` | 통합 테스트 |

## 실습 환경

- Python 3.11+ (권장: [uv](https://docs.astral.sh/uv/))
- Docker / Docker Compose (PostgreSQL 17 + pgvector)
- LLM: OpenAI 호환 API (상용 API 또는 로컬 LLM 서버)
- 임베딩: 임베딩 API (예: bge-m3)

```bash
cp .env.example .env        # API 주소·모델·DB 포트 설정
docker compose up -d        # PostgreSQL 기동 (최초 1회 Northwind·세무 합성 데이터 자동 적재)
bash scripts/check_db.sh    # Northwind 적재 확인
bash scripts/load_taxos.sh  # 세무 합성 데이터 (재)적재 + 행 수 확인
```

| 데이터베이스 | 용도 |
|---|---|
| `ontology` | 온톨로지 엔진 메타데이터·업무 상태·감사 로그 |
| `northwind` | Northwind 샘플 데이터 ([datasets/northwind](datasets/northwind/)) |
| `taxos` | 세무 사무소 합성 데이터 ([datasets/taxos](datasets/taxos/)) |

기본 포트는 `5434`입니다. 이미 사용 중이면 `.env`의 `POSTGRES_PORT`를 바꾸세요.

## 데이터 안내

공개 예제는 공개 샘플 DB(Northwind) 또는 합성 데이터만 사용합니다. 실제 고객 데이터는 포함하지 않습니다.
