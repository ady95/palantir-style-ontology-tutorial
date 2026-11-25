# 01. 전통적인 온톨로지 (PART 2)

RDF·OWL·SPARQL·SHACL로 세무 사무소 도메인을 표현하고 질의·추론·검증합니다.
데이터는 [`datasets/taxos`](../datasets/taxos/) 합성 데이터의 CSV를 그대로 씁니다(PostgreSQL 불필요).

## 준비

```bash
uv sync                 # 저장소 루트에서 1회 (rdflib, owlrl, pyshacl, owlready2, pytest)
java -version           # 04번 HermiT 추론기에만 필요 (Java 8+)
```

## 실습 순서

| 파일 | 장 | 내용 |
|---|---|---|
| [`ontology/taxos.ttl`](ontology/taxos.ttl) | 05·06 | 클래스·속성·공리(TBox). 분리 공리, "법인 ⊑ 일반과세자", 역관계, 정의 클래스 |
| [`01_build_graph.py`](01_build_graph.py) | 07 | CSV → RDF 인스턴스(ABox) 변환, `output/taxos_data.ttl` 저장 |
| [`02_sparql_queries.py`](02_sparql_queries.py) | 08 | SPARQL: 유형별 고객 수, **기한 임박 + 자료 미제출 고객**, 미등록 신고, 업무량 초과 담당자 |
| [`03_reasoning_owlrl.py`](03_reasoning_owlrl.py) | 08 | OWL-RL 추론 전후 비교 (순수 Python) |
| [`04_reasoning_hermit.py`](04_reasoning_hermit.py) | 08 | OWL DL 추론기 HermiT로 일관성 검사·분류, 공리 위반 검출 |
| [`shapes/taxos_shapes.ttl`](shapes/taxos_shapes.ttl) | 08 | SHACL 데이터 제약 |
| [`05_shacl_validation.py`](05_shacl_validation.py) | 08 | 정상 데이터·망가뜨린 데이터 검증 |

```bash
cd 01-traditional-ontology
uv run python 01_build_graph.py
uv run python 02_sparql_queries.py
uv run python 03_reasoning_owlrl.py
uv run python 04_reasoning_hermit.py
uv run python 05_shacl_validation.py
cd .. && uv run pytest -q          # 회귀 테스트
```

## 실행 결과 (기본 데이터, 3060 실습 서버 기준)

| 단계 | 결과 |
|---|---|
| 그래프 생성 | 인스턴스 트리플 17,261개 (0.9초) |
| SPARQL Q2 | 기한 임박 + 자료 미제출 영업 고객 **92명** (SQL 결과와 동일) |
| SPARQL Q3 | 자료 현황 미등록 현재 신고 6건 |
| OWL-RL 추론 | 17,465 → 28,436 트리플 (9.5초). `tx:Customer` 0 → 200, 법인 66곳이 `tx:GeneralTaxpayer`로 추론, `tx:LateFiler` 52명 |
| HermiT | 일관성 OK (1.6초), 분류 결과가 OWL-RL과 일치. "간이과세자인 법인"을 넣으면 모순 검출 |
| SHACL | 정상 데이터: 위반 0, 경고 6. 망가뜨린 데이터: 위반 4건 모두 검출 |

## 이 실습에서 짚을 점

**1. 명시하지 않은 사실을 추론이 채운다.**
그래프 생성 시 고객에게 `tx:Customer`를, 법인에게 `tx:GeneralTaxpayer`를 일부러 붙이지 않았습니다.
`rdfs:subClassOf`와 "법인 ⊑ 일반과세자" 공리로부터 추론기가 채웁니다. 정의 클래스(`tx:LateFiler`)는
위험 이벤트 데이터만으로 고객을 자동 분류합니다.

**2. Open World Assumption — 기록이 없다고 미제출은 아니다.**
Q3의 `FILTER NOT EXISTS`는 "그래프에 없으면 없다"고 보는 닫힌 세계식 질의입니다. OWL 추론기는
제출 기록이 없다는 이유로 "미제출"을 결론 내리지 않습니다. 이 실습에서는 이 6건을 SHACL **경고**로
분리해 "확인 필요"로 다룹니다. 운영형 온톨로지에서는 `미요청·요청됨·제출완료·해당없음`처럼
**명시적 상태**로 관리하는 이유입니다.

**3. OWL 공리와 SHACL 제약은 역할이 다르다.**
"간이과세자인 법인"은 OWL에서는 **모순(inconsistency)** 으로, SHACL에서는 **위반(violation)** 으로 검출됩니다.
OWL은 무엇이 참인지 추론하고, SHACL은 데이터가 규칙을 지키는지 검사합니다.

**4. 추론기마다 지원 범위가 다르다.**
HermiT는 OWL 2 datatype map에 없는 `xsd:date`를 지원하지 않아, HermiT에 넘길 때만 날짜 값을 제외합니다
(`for_dl_reasoner`). OWL-RL(owlrl)은 규칙 기반이라 날짜를 그대로 둘 수 있지만 표현력이 제한됩니다.

**5. 여기까지는 "조회·추론·검증"이다.**
미제출 고객을 찾았지만 자료 요청, 담당자 배정, 결과 반영은 이 계층에 없습니다.
이것이 PART 3 이후 Operational Ontology(Function·Action·Policy)로 확장하는 출발점입니다.
