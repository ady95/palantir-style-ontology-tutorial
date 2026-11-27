"""19장. Ontology Registry 테스트

- 버전 비교(diff)·권장 버전·checksum: DB 없이
- 등록·활성화·롤백: Registry DB(DATABASE_URL)에서 바깥 트랜잭션 안에 실행하고 롤백
실행: uv run pytest -q tests/test_minionto_registry.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto.registry import REGISTRY_SCHEMA, Package, Registry, RegistryError, diff, suggest_version  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
BASE = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
SOURCES = Package.from_file(TAXOS).function_sources
AUTHOR = {"id": "A001", "type": "human"}
REVIEWER = {"id": "A005", "type": "human"}


def variant(edit, version: str | None = None) -> Package:
    raw = copy.deepcopy(BASE)
    edit(raw)
    if version:
        raw["ontology"]["version"] = version
    return Package.from_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), SOURCES)


def levels(changes):
    return {(c.level, c.path) for c in changes}


BASE_PKG = Package.from_file(TAXOS)


# ================================================================ diff (DB 없이)
def test_same_definition_has_no_changes():
    assert diff(BASE_PKG.onto, variant(lambda r: None).onto) == []


def test_property_changes():
    def edit(r):
        props = r["object_types"]["Customer"]["properties"]
        props["email"] = {"type": "string"}                         # 추가 → compatible
        del props["phone"]                                          # 삭제 → breaking
        props["industry"]["column"] = "industry_name"               # 물리 매핑 → compatible
        props["risk_level"]["values"] = ["낮음", "높음", "매우높음"]   # 값 삭제 → breaking, 추가 → compatible
    ch = diff(BASE_PKG.onto, variant(edit).onto)
    assert ("compatible", "object_types.Customer.properties.email") in levels(ch)
    assert ("breaking", "object_types.Customer.properties.phone") in levels(ch)
    assert ("compatible", "object_types.Customer.properties.industry") in levels(ch)
    risk = [c for c in ch if c.path.endswith("risk_level")]
    assert {c.level for c in risk} == {"breaking", "compatible"}
    assert ch[0].level == "breaking"                               # 영향이 큰 것부터 정렬


def test_action_parameter_and_policy_changes():
    def edit(r):
        act = r["actions"]["request_missing_documents"]
        act["parameters"]["channel"] = {"type": "enum", "values": ["문자", "메일"]}       # 필수 → breaking
        act["parameters"]["urgent"] = {"type": "boolean", "required": False}          # 선택 → compatible
        act["permission"]["roles"] = ["세무사", "실무자"]                               # 권한 → policy
    ch = levels(diff(BASE_PKG.onto, variant(edit).onto))
    p = "actions.request_missing_documents"
    assert ("breaking", f"{p}.parameters.channel") in ch
    assert ("compatible", f"{p}.parameters.urgent") in ch
    assert ("policy", f"{p}.permission") in ch


def test_link_rename_is_breaking_but_key_change_is_not():
    def rename(r):
        r["link_types"]["customer_tax_returns"]["forward_name"] = "returns"
    assert ("breaking", "link_types.customer_tax_returns") in levels(diff(BASE_PKG.onto, variant(rename).onto))

    def table(r):
        r["object_types"]["Customer"]["source"]["table"] = "customer_master"
    ch = diff(BASE_PKG.onto, variant(table).onto)
    assert [c.level for c in ch] == ["compatible"]


def test_state_machine_changes_are_policy():
    def edit(r):
        tr = r["object_types"]["TaxReturn"]["state_machine"]["transitions"]
        tr["complete_filing"]["from"] = ["검토중", "자료수집중"]                   # 전이 변경
        del tr["return_to_collection"]                                             # 전이 삭제
        r["object_types"]["Customer"]["state_machine"] = {                         # 상태 머신 추가
            "property": "business_status", "initial": ["영업"], "terminal": ["폐업"], "transitions": {}}
    ch = {(c.level, c.path, c.message) for c in diff(BASE_PKG.onto, variant(edit).onto)}
    sm = "object_types.TaxReturn.state_machine.transitions"
    assert ("policy", f"{sm}.complete_filing", "전이 변경 검토중 → 신고완료  ⇒  검토중·자료수집중 → 신고완료") in ch
    assert ("policy", f"{sm}.return_to_collection", "전이 삭제") in ch
    assert ("policy", "object_types.Customer.state_machine", "상태 머신 추가") in ch


def test_suggest_version():
    assert suggest_version("0.1.0", []) == "0.1.1"
    assert suggest_version("0.1.0", diff(BASE_PKG.onto, variant(
        lambda r: r["object_types"]["Customer"]["properties"].update(email={"type": "string"})).onto)) == "0.2.0"
    assert suggest_version("0.1.0", diff(BASE_PKG.onto, variant(
        lambda r: r["object_types"]["Customer"]["properties"].pop("phone")).onto)) == "1.0.0"


def test_checksum_covers_function_code():
    same = Package.from_text(BASE_PKG.source_yaml, SOURCES)
    changed = Package.from_text(BASE_PKG.source_yaml, {"functions": SOURCES["functions"] + "\n# 수정\n"})
    assert same.checksum == BASE_PKG.checksum
    assert changed.checksum != BASE_PKG.checksum


# ================================================================ Registry (DB)
@pytest.fixture
def reg():
    r = Registry()
    try:
        r.conn
    except Exception:  # noqa: BLE001
        pytest.skip("Registry DB에 연결할 수 없습니다")
    with r.conn.transaction(force_rollback=True):
        r.conn.execute(f"DELETE FROM {REGISTRY_SCHEMA}.ontology_versions WHERE name = 'taxos'")  # 롤백됨
        yield r
    r.close()


def test_register_is_idempotent_and_versions_are_immutable(reg):
    first = reg.register(TAXOS, actor=AUTHOR)
    assert first.created and first.version.status == "draft" and first.base is None
    again = reg.register(TAXOS, actor=AUTHOR)
    assert not again.created and again.version.checksum == first.version.checksum
    tampered = variant(lambda r: r["object_types"]["Customer"]["properties"].pop("phone"))   # 같은 버전, 다른 내용
    with pytest.raises(RegistryError, match="version 을 올리세요"):
        reg.register(tampered, actor=AUTHOR)


def test_activation_gate(reg):
    v = BASE["ontology"]["version"]
    reg.register(TAXOS, actor=AUTHOR)
    with pytest.raises(RegistryError, match="사람만"):
        reg.activate("taxos", v, actor={"id": "agent-01", "type": "agent"})
    with pytest.raises(RegistryError, match="등록자 본인"):
        reg.activate("taxos", v, actor=AUTHOR)
    reg.activate("taxos", v, actor=REVIEWER)                       # DB 대조 검사까지 통과
    active = reg.get("taxos")
    assert active.status == "active" and active.activated_by["id"] == "A005"


def test_breaking_change_needs_explicit_permission_and_rollback(reg):
    v1 = BASE["ontology"]["version"]
    reg.register(TAXOS, actor=AUTHOR)
    reg.activate("taxos", v1, actor=REVIEWER)

    v2 = variant(lambda r: r["object_types"]["Customer"]["properties"].pop("phone"), version="9.0.0")
    res = reg.register(v2, actor=AUTHOR)
    assert res.base == v1 and res.suggested_version.startswith(str(int(v1.split(".")[0]) + 1))
    assert any(c.level == "breaking" for c in res.changes)
    with pytest.raises(RegistryError, match="깨는 변경"):
        reg.activate("taxos", "9.0.0", actor=REVIEWER)
    reg.activate("taxos", "9.0.0", actor=REVIEWER, allow_breaking=True)
    assert reg.get("taxos").version == "9.0.0"
    assert reg.get("taxos", v1).status == "retired"

    reg.activate("taxos", v1, actor=REVIEWER)                      # retired 재활성화 = 롤백
    statuses = {r["version"]: r["status"] for r in reg.history("taxos")}
    assert statuses == {v1: "active", "9.0.0": "retired"}


def test_registered_version_runs_with_stored_function_code(reg):
    v = BASE["ontology"]["version"]
    reg.register(TAXOS, actor=AUTHOR)
    reg.activate("taxos", v, actor=REVIEWER)
    client = reg.get("taxos").client(today="2027-01-18")
    try:
        client.conn
    except Exception:  # noqa: BLE001
        pytest.skip("taxos DB에 연결할 수 없습니다")
    assert client.base_dir is None                                 # 디스크의 functions.py 를 읽지 않는다
    try:
        assert isinstance(client.functions.call("has_capacity", accountant="A001"), bool)
        assert client.functions._module("functions").__file__.startswith("<registry:")
    finally:
        client.close()
