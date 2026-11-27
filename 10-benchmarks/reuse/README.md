# 엔진 재사용성 측정 (35장)

메타데이터(YAML 정의)만 바꿔 같은 엔진을 여러 도메인에 쓸 수 있는지 잰다.
세무(taxos), Northwind 에 더해 세 번째 도메인 **설비 정비(maintenance)** 를 정의·데이터·작은 Function 만으로 올렸다.

| 파일 | 내용 |
|---|---|
| [`measure.py`](measure.py) | 코드 비중(엔진·정의·도메인 코드·스키마·응용), 엔진 안의 도메인 흔적(AST), 장별 변경량(git) |
| [`smoke.py`](smoke.py) | 같은 범용 기능 13가지를 세 도메인에 — 정의만 보고 돌린다. 에이전트·API·SDK 글에 다른 도메인 단어가 섞였는지도 찾는다 |
| [`maintenance_scenario.py`](maintenance_scenario.py) | 설비 정비 업무: 점검 지시(AI) → 고장 신고 → 배정 → 승인 → 작업 완료(되쓰기), 막히는 요청, 권한 정책, `--llm` |
| `results/phase1_*.txt` | 1단계 — 엔진을 고치기 전 (커밋 834c637) |
| `results/phase2_*.txt`, `results/measure.json` | 2단계 — 엔진 개선 뒤 |

```bash
bash scripts/load_maintenance.sh                     # 설비 정비 합성 데이터 (DB maintenance)
uv run python 10-benchmarks/reuse/measure.py         # DB 불필요
uv run python 10-benchmarks/reuse/smoke.py
uv run python 10-benchmarks/reuse/maintenance_scenario.py [--llm]
uv run pytest -q tests/test_reuse_maintenance.py
```

1단계에서 드러난 것과 2단계에서 고친 것

| 드러난 것 | 고친 것 (엔진) |
|---|---|
| 권한 정책에 `$actor.id·role·type` 밖의 실행자 정보(라인)를 쓸 수 없음 | `actor_profile` — 실행자 id 로 찾은 객체의 속성을 `$actor.<속성>` 으로 (호출자가 준 값은 버림) |
| `$params.technician.line` 이 검증을 통과하고 실행 때 invalid | 검증기가 파라미터 안의 속성 참조를 오류로 잡음 (Function 으로 계산) |
| LLM 시스템 프롬프트·MCP 지침·도구 설명 예시·SDK 주석·권한 거부 문구에 세무 표현 | 도메인 중립 문구 + 컨텍스트 첫 줄에 정의의 표시 이름·설명 |
| 감사 추적이 세무의 `notifications` 테이블 이름을 직접 씀 | 객체 타입의 `event_property` 선언으로 발송 결과 객체를 찾음 |

엔진에 남은 세무 전용 모듈: `workflow.py`(27장 업무 그래프), `reasoning.py`(30장 고객 분류 동기화), `cli.py` 의 `workflow`·`reason` 명령.
