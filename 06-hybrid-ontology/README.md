# 06. 하이브리드 온톨로지 (PART 7)

PART 2의 의미 모델(OWL, `01-traditional-ontology/ontology/taxos.ttl`)과 PART 4~의 운영형 정의
(MiniOnto, `03-ontology-engine/ontologies/taxos/ontology.yaml`)를 잇습니다.

| 파일 | 장 | 내용 |
|---|---|---|
| [`taxos_mapping.yaml`](taxos_mapping.yaml) | 29 | 매핑: 객체 타입↔클래스, 속성 값↔하위 클래스, 속성↔데이터 속성, 링크↔객체 속성, 한쪽에만 있는 것과 이유 |
| [`29_mapping.py`](29_mapping.py) | 29 | 매핑 검증 → 공리 확인 → RDF 내보내기 → 07장 그래프와 비교 → 실행자별 내보내기 → OWL 에서 정의 초안 |

```bash
uv run python 06-hybrid-ontology/29_mapping.py
cd 03-ontology-engine
uv run python -m minionto mapping check ../06-hybrid-ontology/taxos_mapping.yaml --db
uv run python -m minionto mapping export ../06-hybrid-ontology/taxos_mapping.yaml --actor A001,세무사,human -o taxos.ttl
uv run python -m minionto mapping draft ../06-hybrid-ontology/taxos_mapping.yaml -o draft.yaml
```

## 실행 결과 (기본 데이터)

| 단계 | 결과 |
|---|---|
| 매핑 검증 | 오류 0. 운영형 객체 타입 8/9·속성 29/59·링크 10/12 매핑, 나머지는 모두 이유와 함께 제외. OWL 클래스 18/20(정의 클래스 2개 제외), 속성 34/34 |
| 공리 확인 | 분리 3개·포함 1개 모두 위반 0 ("법인 ⊑ 일반과세자"는 DB CHECK 제약도 지킨다) |
| RDF 내보내기 | 17,261 트리플 — 07장 CSV 그래프와 트리플 단위로 같음, SPARQL Q2 92명 동일 |
| 실행자별 | 세무사 17,261 / 실무자(A003) 3,823 (고객 43명) / AI 에이전트 17,061 (사업자등록번호 0) |
| 정의 초안 | 객체 타입 8/9, 링크 10/12, 속성 13/28 (이름까지 같음 10) — 도메인 없는 속성 8개는 사람이 정해야 함 |

- 운영형이 원본이다. 데이터는 운영 → RDF 한 방향으로만 흐른다.
- 매핑 형식은 이 책의 자체 설계다 (R2RML·RML 같은 표준 매핑 언어를 단순화한 모양).
