"""17장. Object Engine · Link Engine 테스트

- SQL 생성 테스트: DB 없이 실행된다. 경로 → 조인, some/none → EXISTS, 다대다 → 연결 테이블
- 실행 테스트: 엔진 결과를 '직접 작성한 SQL' 결과와 비교한다 (DB에 연결할 수 없으면 건너뜀)
실행: uv run pytest -q tests/test_minionto_query.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import OntologyClient, QueryError  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
NORTHWIND = ENGINE / "ontologies" / "northwind" / "ontology.yaml"
AS_OF, CURRENT_DUE = "2027-01-18", "2027-01-25"


@pytest.fixture(scope="module")
def tx():
    c = OntologyClient.open(TAXOS, today=AS_OF)
    yield c
    c.close()


@pytest.fixture(scope="module")
def nw():
    c = OntologyClient.open(NORTHWIND)
    yield c
    c.close()


# ================================================================ SQL 생성 (DB 불필요)
def test_single_valued_path_becomes_left_join(tx):
    q = tx.objects("TaxReturn").where(
        {"field": "customer.accountant.max_workload", "op": "lt", "value": 45},
        **{"customer.risk_level": "높음"})
    s = q.sql()
    assert s.count("LEFT JOIN") == 2            # customer 조인은 한 번만 (경로 공유)
    assert '"accountants"' in s and "45" in s


def test_some_none_become_exists(tx):
    s = tx.objects("TaxReturn").where(
        {"some": {"link": "submissions", "where": {"field": "status", "op": "eq", "value": "미요청"}}}
    ).where({"none": {"link": "tasks"}}).sql()
    assert s.count("EXISTS") == 2 and "NOT EXISTS" in s


def test_to_many_path_cannot_be_compared(tx):
    with pytest.raises(QueryError, match="some/none"):
        tx.objects("TaxReturn").where(**{"submissions.status": "미요청"}).sql()


def test_invalid_enum_value(tx):
    with pytest.raises(QueryError, match="허용 값"):
        tx.objects("TaxReturn").where(status="자료수집").sql()


def test_unknown_property_and_link(tx):
    with pytest.raises(QueryError, match="속성이 없습니다"):
        tx.objects("Customer").where(grade="A").sql()
    with pytest.raises(QueryError, match="링크가 없습니다"):
        tx.objects("Customer").pivot("orders")


def test_function_condition_is_not_sql(tx):
    with pytest.raises(QueryError, match="Function"):
        tx.objects("Customer").where({"function": "has_capacity", "args": {}}).sql()


def test_values_are_quoted_not_injected(tx):
    s = tx.objects("Customer").where(company_name="x' OR '1'='1").sql()
    assert "'x'' OR ''1''=''1'" in s


def test_params_binding(tx):
    q = tx.objects("Customer").where({"field": "risk_level", "op": "eq", "value": "$params.level"})
    with pytest.raises(QueryError, match=r"\$params.level"):
        q.sql()
    assert "'높음'" in q.bind(level="높음").sql()


def test_many_to_many_uses_through_table(nw):
    s = nw.objects("Product").where({"some": {"link": "orders"}}).sql()
    assert '"order_details"' in s and '"orders"' in s


def test_self_link_pivot_direction(nw):
    s = nw.objects("Employee").where(last_name="Fuller").pivot("direct_reports").sql()
    # 부하 직원(t0) 기준: 내 상사(t1)가 Fuller 인가 → t1.employee_id = t0.reports_to
    assert '"t1"."employee_id" = "t0"."reports_to"' in s


def test_enum_order_follows_definition(tx):
    s = tx.objects("Customer").order_by("-risk_level").sql()
    assert "array_position(ARRAY['낮음', '보통', '높음']" in s


# ================================================================ 실행 (DB 필요)
@pytest.fixture(scope="module")
def db(tx):
    try:
        tx.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    return tx


def raw(client, query: str) -> list[dict]:
    return client.conn.execute(query).fetchall()


def test_q2_matches_hand_written_sql(db):
    current_open = {"all": [
        {"field": "status", "op": "eq", "value": "자료수집중"},
        {"field": "due_date", "op": "gte", "value": "$today"},
        {"field": "due_date", "op": "lte", "value": CURRENT_DUE},
        {"some": {"link": "submissions", "where": {"field": "status", "op": "in", "value": ["미요청", "요청됨"]}}},
    ]}
    engine = {o.pk for o in db.objects("Customer").where(business_status="영업")
              .where({"some": {"link": "tax_returns", "where": current_open}})}
    expected = {r["customer_id"] for r in raw(db, f"""
        SELECT DISTINCT c.customer_id FROM customers c
        JOIN tax_returns r ON r.customer_id = c.customer_id
        JOIN document_submissions s ON s.return_id = r.return_id
        WHERE c.status = '영업' AND r.status = '자료수집중'
          AND r.due_date BETWEEN '{AS_OF}' AND '{CURRENT_DUE}'
          AND s.status IN ('미요청', '요청됨')""")}
    assert engine == expected and len(engine) == 92        # PART 2 SPARQL 결과와 같다


def test_q3_unregistered_returns(db):
    q = db.objects("TaxReturn").where(due_date=CURRENT_DUE).where({"none": {"link": "submissions"}})
    expected = {r["return_id"] for r in raw(db, f"""
        SELECT r.return_id FROM tax_returns r
        LEFT JOIN document_submissions s ON s.return_id = r.return_id
        WHERE r.due_date = '{CURRENT_DUE}' AND s.submission_id IS NULL""")}
    assert {o.pk for o in q} == expected and q.count() == 6


def test_workload_count_by(db):
    rows = db.objects("Customer").count_by("accountant.accountant_id", "accountant.max_workload")
    over = {r["accountant.accountant_id"] for r in rows if r["count"] > r["accountant.max_workload"]}
    assert over == {"A002", "A003"}


def test_object_navigation(db):
    ret = db.objects("TaxReturn").where(status="자료수집중").order_by("return_id").first()
    assert ret.customer.pk == ret.customer_id
    assert ret.customer.accountant.pk == ret.customer.accountant_id
    subs = ret.submissions.all()
    assert subs and all(s.return_id == ret.pk for s in subs)
    assert ret.submissions.count() == raw(db, f"SELECT count(*) n FROM document_submissions "
                                              f"WHERE return_id = '{ret.pk}'")[0]["n"]


def test_pivot_chain(db):
    q = db.objects("Accountant").where(accountant_id="A002").pivot("customers").pivot("tax_returns")
    expected = raw(db, """SELECT count(*) n FROM tax_returns r JOIN customers c USING (customer_id)
                          WHERE c.accountant_id = 'A002'""")[0]["n"]
    assert q.count() == expected


def test_get_and_missing(db):
    assert db.get("Accountant", "A001").pk == "A001"
    assert db.get("Accountant", "A999") is None


def test_northwind_links(nw):
    try:
        nw.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    fuller = nw.objects("Employee").where(last_name="Fuller").one()
    reports = fuller.direct_reports.all()
    expected = raw(nw, f"SELECT count(*) n FROM employees WHERE reports_to = {fuller.pk}")[0]["n"]
    assert len(reports) == expected > 0
    assert all(e.manager == fuller for e in reports)
    # 업무 속성 'title'(직함)이 엔진 기능 이름에 가려지지 않는다
    assert fuller.title == fuller["title"] != fuller.label
    germany = nw.objects("Customer").where(country="Germany").pivot("orders").pivot("products").count()
    expected = raw(nw, """SELECT count(DISTINCT d.product_id) n FROM order_details d
                          JOIN orders o USING (order_id) JOIN customers c USING (customer_id)
                          WHERE c.country = 'Germany'""")[0]["n"]
    assert germany == expected


def test_engine_matches_sparql(db):
    """PART 2 SPARQL(RDF 그래프)과 엔진(운영 DB)의 결과 ID 집합이 같다 (examples/17_compare_sparql.py)"""
    sys.path.insert(0, str(ENGINE / "examples"))
    compare = __import__("17_compare_sparql").compare
    for key, (sparql, engine) in compare().items():
        assert sparql == engine, key
