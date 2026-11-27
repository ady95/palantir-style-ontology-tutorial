# 08. AI Native Tax OS (PART 8, 33·34장)

세무 사무소 업무를 하나의 운영형 온톨로지(`03-ontology-engine/ontologies/taxos`)로 운영한다. 데이터는 합성 데이터다.

| 파일 | 장 | 내용 |
|---|---|---|
| [`business_rules.yaml`](business_rules.yaml) | 33 | 업무 규칙 목록 21개: 문장 · 종류 · 구현 위치 · 확인 시나리오 |
| [`check_rules.py`](check_rules.py) | 33 | 규칙 목록 ↔ 정의 ↔ 동작 대조, 정의에는 있는데 목록에 없는 규칙 찾기 |
| [`knowledge/`](knowledge/) | 34 | 상담용 안내 문서 9개 (실습용으로 단순화한 합성 안내문) |
| [`consult.py`](consult.py) | 34 | 상담 응대 에이전트: 규칙·엔진으로 거르기 → 업무 데이터 + 문서 검색(임베딩) → 답변 → 검사 → Action |
| [`34_consult.py`](34_consult.py) | 34 | 문의 16건 처리와 평가 (경로 · 사실 · 문서 검색), `--llm --repeat 3` |
| [`daily.py`](daily.py) | 34 | Tax OS 의 하루: 위험 분류 → 상담 → 자료 요청 → 재배정 → 승인 → 고객 알림 → 감사 로그 |

```bash
uv run python 08-tax-os-project/check_rules.py
uv run pytest -q tests/test_tax_os_rules.py
uv run python 08-tax-os-project/34_consult.py [--llm --repeat 3]     # 34장 상담 응대
uv run python 08-tax-os-project/daily.py [--llm]                     # 34장 하루 업무
```

문서 검색은 `.env` 의 임베딩 API(`EMBED_BASE_URL`, `EMBED_SERVER_MODEL`, OpenAI 호환 `/v1/embeddings`)를 쓰고,
없으면 단어 겹침으로 찾는다. 실습용 문의 16건은 마이그레이션 `034_inquiry_samples.sql` 로 들어간다.

세무 정의 v0.8.0 (33장)에서 바뀐 것
- 고객 문의(Inquiry) 객체와 Action 2개 (문의 답변 · 담당자에게 넘기기) — 34장의 상담 응대 에이전트가 쓴다
- 검토 시작 조건에 "자료 현황 등록" 추가 — 자료 현황 행이 없는 신고 6건이 검토를 시작할 수 있던 빈틈(28·30장)을 막는다.
  조회로 표현한 조건(`some: submissions`)과 정확한 Function 조건(`documents_registered`)을 함께 둔다
