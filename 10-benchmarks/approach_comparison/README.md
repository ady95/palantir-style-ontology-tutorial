# 31장. 네 방식 비교 — 같은 요구사항, 네 가지 구현

| 방식 | 파일 | 규칙·권한이 있는 곳 |
|---|---|---|
| A. 일반 DB·API | `a_sql.py` | 서비스 함수의 코드 (요구사항을 빠짐없이 구현한 '성실한 개발자' 버전) |
| B. 전통적 온톨로지 | `b_rdf.py` | RDF 그래프(07장 ETL) + SPARQL·OWL-RL·SHACL. 업무 실행은 SPARQL UPDATE — 그래프만 바뀜 |
| C. 운영형 온톨로지 | `c_onto.py` | `ontology.yaml` + MiniOnto 엔진 |
| D. 하이브리드 | `d_hybrid.py` | C + 매핑·추론 (29·30장) |

| 과제 | 내용 |
|---|---|
| T1 | 공통 업무 질의 (기한 임박 + 미제출 자료가 남은 영업 고객), DB 변경 뒤 최신성 |
| T2 | 반복 위험 고객 분류 |
| T3 | 잘못된 값 3종을 막는가·찾는가, 자료 현황이 없는 신고 |
| T4 | 자료 요청 실행 규칙 5사례 (정상·휴업·진행 중·고위험 AI·신고완료) |
| T5 | 실무자 범위, AI 에이전트의 개인정보 |
| T6 | 누가 무엇을 바꿨나 |
| T7 | 같은 신고에 동시 요청 두 개 (DB 를 실제로 바꾼다) |
| T8 | 규칙 변경("기한후신고 이력 고객의 AI 요청도 승인") 뒤 진입점마다 반영되는가 |

```bash
uv run python 10-benchmarks/approach_comparison/run.py                 # T1~T6, T8 (모두 롤백)
uv run python 10-benchmarks/approach_comparison/run.py --concurrency   # T7 (DB 를 바꾼다)
bash scripts/load_taxos.sh                                             # T7 뒤에 다시 적재
uv run pytest -q tests/test_approach_comparison.py
```

결과는 `results/summary.md`(표)와 `results/results.json`(전체)에 있다. 시간은 실행 환경마다 다르다.
