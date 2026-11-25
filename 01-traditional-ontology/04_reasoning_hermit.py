"""08장(3). OWL DL 추론기(HermiT)로 일관성 검사와 분류

owlready2 에 포함된 HermiT 추론기를 사용합니다. (Java 8 이상 필요)
1) 전체 데이터가 온톨로지 공리와 모순 없는지(일관성) 검사하고, 정의 클래스로 고객을 분류한다.
2) "간이과세자인 법인"을 일부러 넣어, 공리 위반이 불일치(inconsistency)로 검출되는 것을 확인한다.

실행: uv run python 01-traditional-ontology/04_reasoning_hermit.py
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from owlready2 import OwlReadyInconsistentOntologyError, World, sync_reasoner
from rdflib import Graph, Literal
from rdflib.namespace import XSD

from common import load_all, load_ontology

TX_IRI = "https://example.org/taxos#"


def for_dl_reasoner(g: Graph) -> Graph:
    """HermiT는 OWL 2 datatype map에 있는 타입만 지원한다. xsd:date는 그 목록에 없으므로
    날짜 값과 xsd:date range 선언을 뺀다. (분류·일관성 검사에는 날짜가 필요 없다)"""
    out = Graph()
    for s, p, o in g:
        if o == XSD.date or (isinstance(o, Literal) and o.datatype == XSD.date):
            continue
        out.add((s, p, o))
    return out


def to_owlready(g: Graph, tmp: Path, world: World):
    """rdflib 그래프 → RDF/XML 파일 → owlready2 로딩 (owlready2는 Turtle을 읽지 못함)"""
    g = for_dl_reasoner(g)
    path = tmp / "graph.owl"
    g.serialize(path, format="xml")
    onto = world.get_ontology(path.as_uri()).load()
    return onto, world.get_namespace(TX_IRI)


def case_full_data(tmp: Path) -> None:
    print("## 1) 전체 데이터 일관성 검사 + 분류")
    world = World()
    _, tx = to_owlready(load_all(), tmp, world)
    t0 = time.perf_counter()
    with world.get_ontology("http://example.org/inferred#"):
        sync_reasoner(world, infer_property_values=False, debug=0)
    print(f"  일관성: OK  (HermiT {time.perf_counter() - t0:.1f}초)")
    print(f"  tx:LateFiler (기한후신고 이력 고객): {len(list(tx.LateFiler.instances()))}명")
    print(f"  tx:UnreachableCustomer (연락두절 이력): {len(list(tx.UnreachableCustomer.instances()))}명")
    corp_general = [c for c in tx.Corporation.instances() if tx.GeneralTaxpayer in c.INDIRECT_is_a]
    print(f"  법인 중 일반과세자로 분류된 수: {len(corp_general)}")


def case_inconsistent(tmp: Path) -> None:
    print("\n## 2) 공리 위반 데이터 검출")
    world = World()
    onto, tx = to_owlready(load_ontology(), tmp, world)
    with onto:
        bad = tx.Corporation("bad_corp")
        bad.is_a.append(tx.SimplifiedTaxpayer)       # 법인 ⊑ 일반과세자, 일반과세자 ⊥ 간이과세자
    try:
        sync_reasoner(world, debug=0)
        print("  (예상과 다름) 모순이 검출되지 않았습니다.")
    except OwlReadyInconsistentOntologyError:
        print("  모순 검출: '간이과세자인 법인'은 공리(법인 ⊑ 일반과세자, 일반과세자 ⊥ 간이과세자)와 충돌합니다.")


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        case_full_data(Path(d))
        case_inconsistent(Path(d))


if __name__ == "__main__":
    main()
