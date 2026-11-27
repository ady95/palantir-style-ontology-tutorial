"""D. 하이브리드 — MiniOnto + 매핑(29장) + 추론(30장)

실행·권한·감사는 운영형(C)과 같다. 다른 점은 두 가지다.
- 분석 질의는 운영 DB 에서 그때그때 내보낸 RDF 에 SPARQL 로 (스냅숏이 낡지 않는다)
- 분류는 OWL 추론으로 정의하고, 결과는 시스템 Action 으로 받아 둔 CustomerSegment 를 조회한다
"""

from __future__ import annotations

import b_rdf
from minionto import OntologyClient
from minionto.reasoning import infer, sync_segments
from minionto.semantic import Mapping, export_rdf

SYSTEM_ACTOR = {"id": "reasoner", "role": "시스템", "type": "system"}


# ---------------------------------------------------------------------- T1 조회 (내보낸 그래프 + SPARQL)
def missing_customers(c: OntologyClient, m: Mapping, as_of: str, due: str, actor: dict) -> list[str]:
    g = export_rdf(c, m, actor=actor)
    return b_rdf.missing_customers(g, as_of, due)


# ---------------------------------------------------------------------- T2 분류 (추론 → 동기화 → 조회)
def sync_classes(c: OntologyClient, m: Mapping) -> dict:
    inf = infer(c, m)
    res = sync_segments(c, m, inf)
    return {"seconds": inf.seconds, "applied": len(res)}


def repeat_risk(c: OntologyClient) -> list[str]:
    q = c.objects("CustomerSegment").where(segment="반복위험", status="유효")
    return sorted(o.customer_id for o in q.all())


# ---------------------------------------------------------------------- T3 검증 (내보낸 그래프 + SHACL)
def validate(c: OntologyClient, m: Mapping, actor: dict) -> tuple[int, int, list[str]]:
    g = export_rdf(c, m, actor=actor)
    g += m.owl                                       # 클래스 대상 제약이 하위 클래스에도 걸리도록 TBox 를 함께
    return b_rdf.validate(g)
