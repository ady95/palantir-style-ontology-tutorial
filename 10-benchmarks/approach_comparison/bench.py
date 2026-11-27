"""31장. 네 방식 비교 실험의 공통 설정

같은 데이터(세무 합성 데이터, 기준일 2027-01-18)와 같은 요구사항을 네 방식으로 구현한다.
    A. 일반 DB·API      (a_sql.py)    — SQL + 손으로 쓴 서비스 함수
    B. 전통적 온톨로지   (b_rdf.py)    — RDF/OWL/SPARQL/SHACL (PART 2)
    C. 운영형 온톨로지   (c_onto.py)   — MiniOnto (PART 4~6)
    D. 하이브리드        (d_hybrid.py) — MiniOnto + 매핑·추론 (29·30장)
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
for p in (ROOT / "03-ontology-engine", ROOT / "01-traditional-ontology", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"
MAPPING = ROOT / "06-hybrid-ontology" / "taxos_mapping.yaml"
AS_OF = "2027-01-18"
DUE = "2027-01-25"

AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}

# T4: 자료 요청 사례와 기대 결과 (AI 에이전트가 요청)
REQUEST_CASES = {
    "R00146": "executed",          # 정상
    "R00360": "blocked",           # 휴업 고객
    "R00548": "blocked",           # 진행 중인 자료요청 업무
    "R00517": "approval",          # 고위험 고객 + AI → 세무사 승인
    "R00001": "blocked",           # 신고완료
}


def timed(fn, *args, repeat: int = 5, **kw):
    """(결과, 중앙값 ms) — 첫 실행 포함 repeat 번"""
    out, ts = None, []
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn(*args, **kw)
        ts.append((time.perf_counter() - t0) * 1000)
    return out, round(statistics.median(ts), 2)
