"""34장. 상담 응대 에이전트 — 규칙·엔진으로 먼저 거르기, 답변 검사, 템플릿 응답 (LLM·임베딩 없이, 롤백)

실행: uv run pytest -q tests/test_tax_os_consult.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(ROOT / "08-tax-os-project"))

from consult import Doc, Knowledge, TemplateResponder, check_answer, handle, open_inquiries  # noqa: E402
from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402

TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


@pytest.fixture
def c():
    cl = OntologyClient.open(TAXOS, today="2027-01-18")
    try:
        cl.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with cl.conn.transaction(force_rollback=True):
        cl.actions.ensure_tables()
        yield cl
    cl.close()


def test_rules_and_engine_route_before_any_answer(c):
    kb = Knowledge(embed=False)
    s = AgentSession(c, AI, max_calls=10_000, max_rows=100)
    qs = {q["inquiry_id"]: q for q in open_inquiries(c)}
    assert len(qs) == 16
    res = {i: handle(s, kb, TemplateResponder(), qs[i]) for i in ("Q00001", "Q00008", "Q00011", "Q00012")}
    assert res["Q00001"].route == "answered" and "매출 세금계산서 내역" in res["Q00001"].answer
    assert (res["Q00008"].route, res["Q00008"].by) == ("escalated", "rule")          # 휴업 고객
    assert (res["Q00011"].route, res["Q00011"].by) == ("escalated", "engine")        # 세금계산 — R16
    assert c.objects("Task").where(task_type="고객연락", created_by="agent").count() == 3
    q = c.get("Inquiry", "Q00001")
    assert q.status == "답변완료" and q.answered_by == "agent-01"


def test_answer_check():
    fx = {"current_return": {"due_date": "2027-01-25"}}
    docs = [Doc("K04", "카드", "조회 기간은 7월 1일~12월 31일로 맞춰 주세요.")]
    assert check_answer("신고 기한은 2027년 1월 25일입니다.", fx) == []
    assert check_answer("2026년 7월 1일부터 조회하세요.", fx, docs) == []              # 문서에 있는 날짜
    assert check_answer("2027년 2월 10일까지 보내세요.", fx, docs) == ["주지 않은 날짜 2027-2-10"]
    assert check_answer("대략 120만 원입니다.", fx) == ["금액·비율이 들어 있음"]
