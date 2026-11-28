"""37장. Q/A 테스트 변환, 검토 기록 적용, 등록 관문 (LLM 불필요)

- Q/A → 테스트 변환, 검토 기록의 patch·rename·reject 와 수정 기록: DB 없이
- 관문(샌드박스·Q/A 테스트·변경 범위)과 Registry 등록: 세무 DB 에서 롤백
실행: uv run pytest -q tests/test_review_gate.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "09-auto-ontology"))

from extract import TAXOS, base_definition  # noqa: E402
from qa import QATest, build_tests, run_test  # noqa: E402
from review import Review, apply_review, gate, reviewed_definition  # noqa: E402

RAW = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
A001 = {"id": "A001", "role": "세무사", "type": "human"}

QA = [
    {"id": "Q001", "task": "검토 시작", "question": "세무사가 자료수집중 신고의 검토를 시작할 수 있나요?", "answer": "예",
     "grounded": True, "source": "manual",
     "case": {"actor_role": "세무사", "target": "TaxReturn", "target_state": "자료수집중", "where": None, "expect": "가능"}},
    {"id": "Q002", "task": "검토 시작", "question": "AI비서가 검토를 시작할 수 있나요?", "answer": "문서에 없음",
     "grounded": False, "source": "manual",
     "case": {"actor_role": "AI비서", "target": "TaxReturn", "target_state": "자료수집중", "where": None, "expect": None}},
    {"id": "Q003", "task": "검토 시작", "question": "신고완료 신고의 검토를 시작할 수 있나요?", "answer": "아니요",
     "grounded": True, "source": "manual",
     "case": {"actor_role": "세무사", "target": "TaxReturn", "target_state": "신고완료", "where": None, "expect": "불가"}},
]


def gold_entry(name: str) -> dict:
    """세무 정의의 Action 하나를 초안 형식으로 (정답과 같은 초안)"""
    d = copy.deepcopy(RAW["actions"][name])
    trans = []
    for ot_name, ot in RAW["object_types"].items():
        t = (ot.get("state_machine") or {}).get("transitions", {}).get(name)
        if t:
            trans.append({"object_type": ot_name, "from": t["from"], "to": t["to"]})
    return {"definition": d, "transitions": trans, "candidate": {"name": "검토 시작", "qa": ["Q001", "Q002", "Q003"]}}


def test_only_grounded_qa_with_expectation_become_tests():
    tests = build_tests(QA)
    assert [t.id for t in tests] == ["Q001", "Q003"]


def test_review_patches_are_logged_and_original_untouched():
    entries = {"begin_review": gold_entry("start_review")}
    review = Review(A001, qa={"Q002": {"verdict": "answer", "answer": "아니요, 사람만 시작합니다",
                                       "case": {"expect": "불가"}, "reason": "업무 기준"}},
                    actions={"begin_review": {"decision": "modify", "rename": "start_review",
                                              "patches": [{"set": "definition.permission.actor_types",
                                                           "value": ["human"], "reason": "AI 는 검토를 시작하지 않는다"}]}})
    out, qa, log = apply_review(entries, QA, review)
    assert list(out) == ["start_review"]
    assert out["start_review"]["definition"]["permission"]["actor_types"] == ["human"]
    assert entries["begin_review"]["definition"]["permission"]["actor_types"] != ["human"]     # 원본 유지
    assert {x["change"] for x in log} == {"answer", "set", "rename"}
    assert all(x["by"] == "A001" for x in log)
    assert next(q for q in qa if q["id"] == "Q002")["grounded"] is True
    assert [t.id for t in build_tests(qa)] == ["Q001", "Q002", "Q003"]


def test_reject_drops_action():
    entries = {"x": gold_entry("start_review")}
    out, _, log = apply_review(entries, QA, Review(A001, actions={"x": {"decision": "reject", "reason": "중복"}}))
    assert out == {} and log[0]["change"] == "reject"


@pytest.fixture
def taxos():
    from minionto import OntologyClient
    c = OntologyClient.open(TAXOS, today="2027-01-18")
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("세무 DB에 연결할 수 없습니다")
    yield c
    c.close()


def test_qa_test_on_gold_definition(taxos):
    with taxos.conn.transaction(force_rollback=True):
        ok = run_test(taxos, QATest("Q003", "검토 시작", "세무사", "TaxReturn", "신고완료", None, "불가"),
                      "start_review", {})
        wrong = run_test(taxos, QATest("Q009", "신고 완료", "실무자", "TaxReturn", "검토중", None, "가능"),
                         "complete_filing", {})                      # 신고 완료는 세무사만 — 틀린 답
        unsure = run_test(taxos, QATest("Q001", "검토 시작", "세무사", "TaxReturn", "자료수집중", None, "가능"),
                          "start_review", {})
        other = run_test(taxos, QATest("Q010", "검토 시작", "세무사", "Customer", None, None, "가능"),
                         "start_review", {})
    assert ok.outcome == "pass" and wrong.outcome == "fail" and other.outcome == "fail"
    assert unsure.outcome in ("pass", "skip")          # 자료가 모두 갖춰진 신고가 데이터에 없으면 판정 보류


def test_gate_blocks_unreviewed_and_passes_after_review(taxos):
    entries = {"start_review": gold_entry("start_review")}
    base = base_definition({"start_review"})
    active = taxos.onto
    data = reviewed_definition(base, entries, "0.9.0")
    first = gate(data, {n: {**e, "decision": None} for n, e in entries.items()}, QA, None, taxos, active)
    by = {c.name: c for c in first.checks}
    assert not first.ok and not by["검토 완료"].ok and by["검증기"].ok and by["변경 범위"].ok

    review = Review(A001, qa={"Q002": {"verdict": "answer", "answer": "아니요", "case": {"expect": "불가"}}},
                    actions={"start_review": {"decision": "approve"}})
    out, qa, _ = apply_review(entries, QA, review)
    second = gate(reviewed_definition(base, out, "0.9.0"), out, qa, review, taxos, active)
    assert second.ok, second.notes()


def test_gate_flags_changes_outside_review(taxos):
    entries = {"start_review": {**gold_entry("start_review"), "decision": "approve"}}
    base = base_definition({"start_review"})
    base["actions"]["update_risk_level"]["permission"]["roles"] = ["세무사", "실무자", "AI비서"]   # 검토 밖 변경
    data = reviewed_definition(base, entries, "0.9.0")
    rep = gate(data, entries, [q for q in QA if q["grounded"]], None, taxos, taxos.onto)
    scope = next(c for c in rep.checks if c.name == "변경 범위")
    assert not scope.ok and any("update_risk_level" in d for d in scope.details)
