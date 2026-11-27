"""29장. 의미 모델(OWL) ↔ 운영형 정의 매핑 테스트

실행: uv run pytest -q tests/test_minionto_semantic.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(ROOT / "01-traditional-ontology"))

from rdflib import URIRef  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.semantic import Mapping, axiom_checks, check, compare_draft, draft_from_owl, export_rdf  # noqa: E402

MAPPING = ROOT / "06-hybrid-ontology" / "taxos_mapping.yaml"
TX = "https://example.org/taxos#"
TAX = {"id": "A001", "role": "세무사", "type": "human"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


@pytest.fixture
def m():
    return Mapping.load(MAPPING)


@pytest.fixture
def client(m):
    c = OntologyClient(m.onto)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    yield c
    c.close()


def test_mapping_is_complete_and_consistent(m):
    rep = check(m)
    assert rep.errors == []
    assert all(mapped + excluded == total for mapped, excluded, total in rep.coverage.values()), rep.coverage
    assert rep.coverage["객체 타입"] == (8, 3, 11) and rep.coverage["링크 타입"] == (10, 4, 14)   # v0.8.0 (30장 분류, 33장 문의)
    assert not [f for f in rep.findings if f.level == "warn"]


def test_check_catches_mistakes(m):
    from minionto.semantic import LinkMap, PropMap
    m.types["Customer"].props.append(PropMap("contracted_at", [URIRef(TX + "riskLevel")]))    # date ↔ string
    m.links["tax_return_tasks"] = LinkMap("tax_return_tasks", URIRef(TX + "aboutReturn"))      # 방향을 잘못
    m.types["Task"].props.append(PropMap("note", [URIRef(TX + "memo")]))                       # OWL 에 없는 속성
    del m.types["Customer"].not_mapped["phone"]                                                # 이유 없이 빠짐
    msgs = [f.message for f in check(m).findings]
    assert any("contracted_at(date) ↔ tx:riskLevel" in x for x in msgs)
    assert any("tax_return_tasks: tx:aboutReturn 의 도메인 tx:Task 에 TaxReturn" in x for x in msgs)
    assert any("tx:memo 은 OWL 에 선언된 속성이 아닙니다" in x for x in msgs)
    assert any("Customer.phone 이 매핑되지 않았고 이유도 없습니다" in x for x in msgs)


def test_export_equals_part2_graph(m, client):
    g = export_rdf(client, m, actor=TAX)
    ref = importlib.import_module("01_build_graph").build()
    assert set(g) == set(ref) and len(g) == 17261


def test_export_follows_policies(m, client):
    risk, reg = URIRef(TX + "riskLevel"), URIRef(TX + "businessRegNo")
    staff = export_rdf(client, m, actor=STAFF)
    mine = {r["customer_id"] for r in client.conn.execute(
        "SELECT customer_id FROM customers WHERE accountant_id = 'A003'").fetchall()}
    assert {str(s).rsplit("/", 1)[1] for s in staff.subjects(risk, None)} == mine
    seen = set(staff.subjects())
    data = "https://example.org/taxos/data/"
    assert all(o in seen for _, _, o in staff if str(o).startswith(data))   # 보이지 않는 객체로 가는 링크 없음
    agent = export_rdf(client, m, actor=AI)
    assert len(list(agent.triples((None, reg, None)))) == 0           # 속성 정책: 에이전트에게 사업자등록번호 없음
    assert len(set(agent.subjects(risk, None))) == 200


def test_axioms_hold_in_data_and_db_enforces_one(m, client):
    res = {a["axiom"]: a for a in axiom_checks(client, m)}
    assert len(res) == 4 and all(a["violations"] == 0 for a in res.values())
    assert res["tx:Corporation rdfs:subClassOf tx:GeneralTaxpayer"]["structural"] is False
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):                 # "법인은 일반과세자" 는 DB 제약이 지킨다
        with client.conn.transaction():
            client.conn.execute("UPDATE customers SET vat_type = '간이과세자' WHERE business_type = '법인'")


def test_draft_from_owl(m):
    d = draft_from_owl(m.owl)
    cust = d["object_types"]["Customer"]["properties"]
    assert {tuple(cust["kind_1"]["values"]), tuple(cust["kind_2"]["values"])} == \
        {("법인", "개인사업자"), ("일반과세자", "간이과세자")}
    assert d["link_types"]["managed_by"]["cardinality"] == "many_to_one"
    assert d["link_types"]["has_tax_return"]["cardinality"] == "one_to_many"
    assert any("LateFiler: 정의 클래스" in n for n in d["notes"])
    cmp = compare_draft(d, m)
    assert cmp["객체 타입"] == (8, 11) and cmp["링크 타입"] == (10, 14)
