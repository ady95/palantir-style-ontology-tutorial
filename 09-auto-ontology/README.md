# 09. Operational Ontology 자동 생성 (PART 9)

LLM 으로 업무 문서에서 운영형 온톨로지의 정의를 만든다. 결과는 언제나 **초안**이며 검증기와 사람의 검토를 거친다.

## 36장 — 업무 매뉴얼·상담 로그에서 규칙과 Action 추출

| 파일 | 내용 |
|---|---|
| [`sources/manual.md`](sources/manual.md) | 세무 사무소 신고 업무 매뉴얼 (실습용 합성 문서, 배경·요령과 규칙이 섞여 있다) |
| [`sources/consult_logs.md`](sources/consult_logs.md) | 상담 로그 발췌 12건 (합성, 고객·직원은 ID) |
| [`gold/gold.yaml`](gold/gold.yaml) | 정답: 규칙 32개(33장 목록 20 + 정의에만 있던 것 12), Action 11개(세무 정의 v0.8.0) |
| [`extract.py`](extract.py) | 분할 → 추출(JSON) → 합치기(임베딩) → DSL 변환 → 검증·수리 |
| [`36_extract.py`](36_extract.py) | 실행과 평가: LLM 채점자로 규칙·Action 짝짓기, 필드 일치, 검증 통과율, 정답과의 판단 일치율 |
| `results/` | 실행별 결과(`run_*.json`)와 초안 Action(`draft_*.yaml`) |

```bash
uv run python 09-auto-ontology/36_extract.py --runs 3      # 매뉴얼 + 상담 로그
uv run python 09-auto-ontology/36_extract.py --no-logs     # 매뉴얼만
uv run pytest -q tests/test_auto_extract.py                # LLM 없는 부분
```

객체 모델(객체·속성·링크·Function·역할·상태 값)은 이미 있다고 보고 세무 정의의 것을 쓴다. 문서에서 만드는 것은 Action 과 상태 전이다.
DSL 변환 예시로는 다른 도메인(Northwind·설비 정비)의 Action 을 보여 줘 정답이 프롬프트에 섞이지 않게 했다.
LLM 은 `.env` 의 OpenAI 호환 API, 임베딩은 `EMBED_BASE_URL`·`EMBED_SERVER_MODEL` 을 쓴다.
