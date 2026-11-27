"""35장. 세 번째 도메인(설비 정비)과 엔진 개선 테스트 — 실행자 정보, 중첩 참조 검사, 도메인 중립 문구 (롤백)

실행: uv run pytest -q tests/test_reuse_maintenance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))

from minionto import OntologyClient  # noqa: E402
from minionto.metamodel import Ontology  # noqa: E402
from minionto.validator import validate  # noqa: E402

MT = ROOT / "03-ontology-engine" / "ontologies" / "maintenance" / "ontology.yaml"
LEAD = {"id": "E01", "role": "정비팀장", "type": "human"}
E02 = {"id": "E02", "role": "정비원", "type": "human"}
E06 = {"id": "E06", "role": "정비원", "type": "human"}


@pytest.fixture
def c():
    cl = OntologyClient.open(MT, today="2027-01-18")
    try:
        cl.conn
    except Exception:  # noqa: BLE001
        pytest.skip("maintenance DB에 연결할 수 없습니다 (scripts/load_maintenance.sh)")
    with cl.conn.transaction(force_rollback=True):
        cl.actions.ensure_tables()
        yield cl
    cl.close()


def raw() -> dict:
    return yaml.safe_load(MT.read_text(encoding="utf-8"))


def errors(data: dict) -> list[str]:
    return [i.message for i in validate(Ontology.model_validate(data), MT.parent).issues if i.level == "error"]


def test_definition_is_valid():
    assert errors(raw()) == []


def test_actor_attribute_needs_profile():
    data = raw()
    del data["actor_profile"]
    assert any("$actor 에는" in m and "actor_profile" in m for m in errors(data))


def test_nested_param_reference_is_rejected():
    data = raw()
    data["actions"]["assign_technician"]["preconditions"][0]["check"] = \
        {"field": "equipment.line", "op": "eq", "value": "$params.technician.line"}
    assert any("파라미터 안의 속성" in m for m in errors(data))


def test_actor_profile_reads_line_from_data_not_caller(c):
    spoofed = {**E02, "line": "B라인"}                 # E02 는 A라인 — 호출자가 준 값은 버린다
    seen = c.objects("Equipment").secure(spoofed).all()
    assert seen and {e.line for e in seen} == {"A라인"}
    assert c.actor_view(spoofed)["line"] == "A라인"
    assert c.actor_view({"id": "nobody", "role": "정비원", "type": "human"})["line"] is None
    assert c.objects("Equipment").secure({"id": "nobody", "role": "정비원", "type": "human"}).count() == 0


def test_line_mate_can_report_and_finish_repair(c):
    target = next(e for e in c.objects("Equipment").where(line="A라인", status="가동").order_by("equipment_id").all()
                  if e.technician_id != "E02" and not c.objects("WorkOrder").where(
                      equipment_id=e.pk, kind="고장수리", status=["접수", "배정", "작업중"]).count())
    r = c.actions.apply("report_failure", target.pk, actor=E02, params={"estimated_cost": 4500000, "note": "누유"})
    assert r.status == "completed"
    wo = c.objects("WorkOrder").where(equipment_id=target.pk, kind="고장수리", status="접수").first()
    assert c.actions.apply("assign_technician", wo.pk, actor=LEAD, params={"technician": "E06"}).status == "rejected"
    assert c.actions.apply("assign_technician", wo.pk, actor=LEAD, params={"technician": "E02"}).status == "completed"
    assert c.actions.apply("start_work", wo.pk, actor=E06).status == "denied"          # 다른 라인
    r = c.actions.apply("start_work", wo.pk, actor=E02)
    assert r.status == "approval_required"
    assert c.actions.decide(r.approval_id, LEAD, approve=True).status == "completed"
    assert c.actions.apply("complete_work", wo.pk, actor=E02).status == "completed"
    eq = c.get("Equipment", target.pk)
    assert eq.status == "가동" and str(eq.last_inspected) == "2027-01-18"


def test_agent_facing_texts_carry_no_domain_words(c):
    from minionto.agent import AgentSession
    from minionto.llm import SYSTEM_PROMPT
    from minionto.mcp_server import GUIDELINES
    for text in (SYSTEM_PROMPT, GUIDELINES):
        assert "세무" not in text and "고객" not in text
    ctx = AgentSession(c, {"id": "agent-mt", "role": "AI비서", "type": "agent"}).context_text()
    assert ctx.startswith("온톨로지 공장 설비 정비 온톨로지 (maintenance v0.2.0)")
    assert "세무" not in ctx
