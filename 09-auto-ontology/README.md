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

## 37장 — Q/A 기반 생성과 전문가 검증·등록 관문

| 파일 | 내용 |
|---|---|
| [`qa.py`](qa.py) | 문서 → Q/A(질문·답·근거·시스템에서 확인할 형태) → 규칙·Action 후보, Q/A → 판단 테스트 |
| [`37_qa.py`](37_qa.py) | 실행과 평가: 36장과 같은 채점 + Q/A 테스트(초안·정답 정의 양쪽에서) |
| [`review.py`](review.py) | 검토 기록 적용(수정 기록), 샌드박스 실행, 등록 관문 6가지 |
| [`review/review_1.yaml`](review/review_1.yaml) | 1회차 초안에 대한 전문가 검토 기록 (저자가 세무 정의를 기준으로 작성) |
| [`37_review.py`](37_review.py) | 관문(검토 전) → 검토 적용 → 관문(검토 후) → Registry 등록·활성화(롤백) → 정답과 비교 |
| `results37/` | 실행별 Q/A(`qa_*.json`), 초안(`draft_*.yaml`), 결과(`run_*.json`) |

```bash
uv run python 09-auto-ontology/37_qa.py --runs 3           # Q/A 기반 생성 (LLM)
uv run python 09-auto-ontology/37_qa.py --retest           # 저장된 결과로 Q/A 테스트만 다시 (LLM 없음)
uv run python 09-auto-ontology/37_review.py                # 검토·관문·등록 (LLM 없음, 모두 롤백)
uv run pytest -q tests/test_review_gate.py
```

등록 관문: 검증기 · 검토 완료(모든 Action 결정, 문서에 답이 없던 질문에 전문가 답) · 변경 범위(활성 버전과의 차이가 검토한 Action 안에만,
breaking 은 명시 허용) · DB 대조 · 샌드박스 실행(항상 롤백) · Q/A 테스트. 모두 통과해야 Registry 에 draft 로 등록하고,
활성화는 등록자가 아닌 다른 사람이 한다(19장 Registry 규칙).
