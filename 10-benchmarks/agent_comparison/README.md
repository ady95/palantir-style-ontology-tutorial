# 28장. 일반 에이전트와 온톨로지 기반 에이전트 비교

같은 LLM, 같은 도구 호출 루프, 같은 업무 규칙(프롬프트)으로 도구만 다른 에이전트를 비교한다.

| 방식 | 도구 | 업무 규칙 |
|---|---|---|
| A. `sql` | `run_sql` 하나 (읽기·쓰기) | 프롬프트 (`agents.RULES`) + 스키마 |
| B. `plain` | 업무 도구 8개 (입력·존재·현재 상태만 검증) | 프롬프트 |
| C. `ontology` | 25장 AgentSession 도구 6개 | 엔진이 강제 |
| D. `graph` | 27장 업무 그래프 (B1 일괄 업무만) | 엔진이 강제 |
| A'·B' `sql_stale`·`plain_stale` | A·B 와 같음 | 규칙 세 개가 빠진 옛 프롬프트 (`agents.STALE_RULES`) |

과제 19개(`scenarios.py`): 조회 R1~R3, 정상 업무 W1·W2, 비정상 요청 N1~N4·N6·N8·N9, 중복 N5,
개인정보 N7, 압박·간접 P1~P3·P5, 일괄 B1. 판정은 실행 전후 업무 테이블의 차이와 답의 내용으로 한다.

```bash
uv run python 10-benchmarks/agent_comparison/run.py --repeat 3
uv run python 10-benchmarks/agent_comparison/run.py --agents graph --only B1 --repeat 3 --append
uv run python 10-benchmarks/agent_comparison/run.py --agents sql_stale,plain_stale --only N3,N4,N6,P2,P3,B1 \
    --repeat 3 --out 10-benchmarks/agent_comparison/results/stale.jsonl
uv run python 10-benchmarks/agent_comparison/report.py                               # results/runs_summary.md
uv run python 10-benchmarks/agent_comparison/report.py 10-benchmarks/agent_comparison/results/stale.jsonl
uv run pytest -q tests/test_agent_comparison.py                                       # 판정기 검증 (LLM 없음)
```

- 실행마다 바깥 트랜잭션을 열고 롤백한다. SQL 에이전트의 트랜잭션 제어 문(BEGIN·COMMIT 등)은 실험 장치가 막는다.
- `results/` 는 실측 결과(2026-11, OpenAI 호환 API의 GPT 계열 모델 하나). 답의 개인정보 원문은 [가림]으로 저장한다.
- 모델·프롬프트가 바뀌면 결과도 바뀐다. 같은 조건에서도 반복마다 달라질 수 있다.
