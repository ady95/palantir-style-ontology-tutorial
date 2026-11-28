"""36장. 추출 파이프라인의 LLM 없는 부분 — 분할, 객체 모델 설명, 초안 정의 조립과 검증 (DB 불필요)

실행: uv run pytest -q tests/test_auto_extract.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "09-auto-ontology"))

from extract import GOLD, assemble, base_definition, check, chunks, object_model  # noqa: E402

from minionto.loader import load  # noqa: E402

GOLD_ACTIONS = {a["name"] for a in yaml.safe_load(GOLD.read_text(encoding="utf-8"))["actions"]}
TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"


def test_chunks_follow_sections_and_size():
    parts = chunks()
    assert {p.source for p in parts} == {"manual", "logs"}
    assert all(p.text.startswith("## ") for p in parts)
    assert len([p for p in chunks(include_logs=False) if p.source == "logs"]) == 0


def test_object_model_has_no_actions():
    text = object_model(load(TAXOS), {"Customer", "TaxReturn"})
    assert "TaxReturn" in text and "Inquiry (" not in text
    assert "request_missing_documents" not in text and "has_capacity" in text


def test_base_definition_drops_gold_actions_and_transitions():
    base = base_definition(GOLD_ACTIONS)
    assert not GOLD_ACTIONS & set(base["actions"])
    assert "record_notification" in base["actions"]                       # 범위 밖 Action 은 남는다
    assert "start_review" not in base["object_types"]["TaxReturn"]["state_machine"]["transitions"]
    assert check(base, []) == {}


def test_check_attributes_errors_to_the_action():
    good = {"definition": {"display_name": "등급 변경", "target": "Customer",
                           "parameters": {"level": {"type": "enum", "values": ["낮음", "보통", "높음"]}},
                           "permission": {"roles": ["세무사"]},
                           "edits": [{"update": "target", "set": {"risk_level": "$params.level"}}]},
            "transitions": []}
    bad_ref = {"definition": {**good["definition"], "edits": [{"update": "target", "set": {"no_such": 1}}]},
               "transitions": []}
    bad_syntax = {"definition": {"display_name": "x", "target": "Customer"}, "transitions": []}   # permission·edits 없음
    errs = check(assemble(base_definition(GOLD_ACTIONS), {"ok": good, "ref": bad_ref, "syn": bad_syntax}),
                 ["ok", "ref", "syn"])
    assert errs["ok"] == []
    assert any("no_such" in e for e in errs["ref"])
    assert errs["syn"] and errs["syn"][0].startswith("문법")


def test_transition_goes_to_state_machine():
    a = {"definition": {"display_name": "검토", "target": "TaxReturn", "permission": {"roles": ["세무사"]},
                        "edits": [{"update": "target", "set": {"status": "검토중"}}]},
         "transitions": [{"object_type": "TaxReturn", "from": ["자료수집중"], "to": "검토중"}]}
    data = assemble(base_definition(GOLD_ACTIONS), {"begin_review": a})
    assert data["object_types"]["TaxReturn"]["state_machine"]["transitions"]["begin_review"]["to"] == "검토중"
    assert check(data, ["begin_review"]) == {"begin_review": []}


def test_linked_transitions_and_old_format():
    from extract import transitions_of
    a = {"definition": {"display_name": "취소", "target": "TaxReturn", "permission": {"roles": ["세무사"]},
                        "edits": [{"update_linked": "submissions", "where": {"field": "status", "op": "eq",
                                                                             "value": "요청됨"},
                                   "set": {"status": "미요청"}}]},
         "transitions": [{"object_type": "DocumentSubmission", "from": ["요청됨"], "to": "미요청"}]}
    data = assemble(base_definition(GOLD_ACTIONS), {"undo_request": a})
    assert data["object_types"]["DocumentSubmission"]["state_machine"]["transitions"]["undo_request"]["to"] == "미요청"
    assert check(data, ["undo_request"]) == {"undo_request": []}
    old = {"definition": {"target": "TaxReturn"}, "transition": {"from": ["검토중"], "to": "자료수집중"}}
    assert transitions_of(old) == [{"object_type": "TaxReturn", "from": ["검토중"], "to": "자료수집중"}]
