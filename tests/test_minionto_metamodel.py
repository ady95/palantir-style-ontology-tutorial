"""16장. MiniOnto 메타모델·검증기 테스트

정상 정의가 통과하는지, 그리고 흔한 실수를 일부러 넣었을 때 검증기가 정확히 잡아내는지 확인한다.
실행: uv run pytest -q tests/test_minionto_metamodel.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import Ontology, Schema, load, validate  # noqa: E402
from minionto.loader import OntologyLoadError  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
NORTHWIND = ENGINE / "ontologies" / "northwind" / "ontology.yaml"


@pytest.fixture
def raw():
    with open(TAXOS, encoding="utf-8") as f:
        return yaml.safe_load(f)


def errors_of(raw_dict) -> list[str]:
    report = validate(Ontology.model_validate(raw_dict))
    return [f"{i.path}: {i.message}" for i in report.errors]


# ---------------------------------------------------------------- 정상 정의
@pytest.mark.parametrize("path", [TAXOS, NORTHWIND], ids=["taxos", "northwind"])
def test_ontology_definitions_are_valid(path):
    report = validate(load(path))
    assert report.ok, report.errors


def test_same_engine_two_domains():
    taxos, nw = load(TAXOS), load(NORTHWIND)
    assert set(taxos.object_types).isdisjoint({"Order", "Product"})
    assert {"Order", "Product"} <= set(nw.object_types)


# ---------------------------------------------------------------- 경로 해석
def test_resolve_single_valued_path():
    res = Schema(load(TAXOS)).resolve("TaxReturn", "customer.accountant.max_workload")
    assert res.object_type == "Accountant" and res.prop.type == "integer" and not res.many


def test_resolve_to_many_path():
    res = Schema(load(TAXOS)).resolve("TaxReturn", "submissions.status")
    assert res.many


def test_many_to_many_and_self_link():
    schema = Schema(load(NORTHWIND))
    assert schema.links["Product"]["orders"].many
    assert schema.links["Employee"]["manager"].target == "Employee"
    assert schema.links["Employee"]["direct_reports"].many


# ---------------------------------------------------------------- 문법 오류 (로딩 단계)
def test_typo_key_is_rejected(raw):
    raw["link_types"]["customer_accountant"]["cardinalty"] = "many_to_one"
    with pytest.raises(ValidationError, match="cardinalty"):
        Ontology.model_validate(raw)


def test_condition_with_two_forms_is_rejected(raw):
    pre = raw["actions"]["complete_filing"]["preconditions"][0]["check"]
    pre["some"] = {"link": "submissions"}
    with pytest.raises(ValidationError, match="정확히 하나"):
        Ontology.model_validate(raw)


def test_enum_without_values_is_rejected(raw):
    del raw["object_types"]["Customer"]["properties"]["risk_level"]["values"]
    with pytest.raises(ValidationError, match="values"):
        Ontology.model_validate(raw)


def test_loader_reports_location(tmp_path, raw):
    raw["object_types"]["Task"]["properties"]["status"]["type"] = "text"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(OntologyLoadError, match="object_types.Task.properties.status.type"):
        load(bad)


# ---------------------------------------------------------------- 참조 오류 (검증 단계)
def test_invalid_enum_literal(raw):
    raw["actions"]["request_missing_documents"]["preconditions"][0]["check"]["value"] = "자료수집"
    assert any("허용 값이 아닙니다" in e for e in errors_of(raw))


def test_link_to_unknown_object_type(raw):
    raw["link_types"]["customer_accountant"]["to"] = "Staff"
    assert any("객체 타입 'Staff'" in e for e in errors_of(raw))


def test_to_many_path_needs_quantifier(raw):
    raw["actions"]["complete_filing"]["preconditions"][0]["check"] = \
        {"field": "submissions.status", "op": "eq", "value": "제출완료"}
    assert any("some/none" in e for e in errors_of(raw))


def test_create_missing_required_property(raw):
    del raw["actions"]["request_missing_documents"]["edits"][1]["values"]["task_type"]
    assert any("필수 속성" in e and "task_type" in e for e in errors_of(raw))


def test_unknown_parameter_reference(raw):
    raw["actions"]["update_risk_level"]["edits"][0]["set"]["risk_level"] = "$params.new_level"
    assert any("new_level" in e for e in errors_of(raw))


def test_undeclared_role(raw):
    raw["actions"]["update_risk_level"]["permission"]["roles"] = ["팀장"]
    assert any("역할 '팀장'" in e for e in errors_of(raw))


def test_primary_key_cannot_be_updated(raw):
    raw["actions"]["update_risk_level"]["edits"][0]["set"]["customer_id"] = "C9999"
    assert any("기본키" in e for e in errors_of(raw))


def test_interface_property_missing(raw):
    raw["object_types"]["RiskEvent"]["implements"] = ["Trackable"]
    assert any("Trackable" in e and "status" in e for e in errors_of(raw))


def test_condition_function_must_return_boolean(raw):
    raw["actions"]["assign_accountant"]["preconditions"][1]["check"]["function"] = "recommend_assignee"
    assert any("boolean" in e for e in errors_of(raw))


# ---------------------------------------------------------------- DB 대조
def _db_url(onto):
    try:
        import psycopg

        from minionto.db import database_url
        url = database_url(onto)
        psycopg.connect(url, connect_timeout=3).close()
        return url
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")


@pytest.mark.parametrize("path", [TAXOS, NORTHWIND], ids=["taxos", "northwind"])
def test_database_matches_definition(path):
    from minionto.db import check_database
    onto = load(path)
    report = check_database(onto, _db_url(onto))
    assert report.ok, report.errors


def test_database_detects_wrong_column(raw):
    from minionto.db import check_database
    bad = copy.deepcopy(raw)
    bad["object_types"]["Customer"]["properties"]["risk_level"]["column"] = "risk_grade"
    bad["object_types"]["TaxReturn"]["properties"]["due_date"]["type"] = "string"
    onto = Ontology.model_validate(bad)
    msgs = [i.message for i in check_database(onto, _db_url(onto)).errors]
    assert any("risk_grade" in m for m in msgs)
    assert any("타입 불일치" in m and "due_date" in m for m in msgs)
