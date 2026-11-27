"""24장. 감사 로그 · 이벤트 구독 · 피드백 테스트

바깥 트랜잭션 안에서 실행하고 롤백한다 (기록을 일부러 고치거나 지우는 점검 테스트 포함).
실행: uv run pytest -q tests/test_minionto_audit.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import Ontology, OntologyClient  # noqa: E402
from minionto.audit import AuditLog  # noqa: E402
from minionto.subscriptions import SubscriptionRunner  # noqa: E402
from minionto.validator import validate  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
RAW = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}


@pytest.fixture
def tx():
    c = OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    with c.conn.transaction(force_rollback=True):
        yield c
    c.close()


def low_risk(c):
    return c.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk


def flow(c):
    """AI 의 자료 요청 → 담당자가 자료를 모두 접수 (자료마다 document_received 이벤트)"""
    ret = low_risk(c)
    req = c.actions.apply(REQ, ret, actor=AI)
    staff = {"id": c.get("TaxReturn", ret).customer.accountant_id, "role": "실무자", "type": "human"}
    docs = [x["pk"] for x in req.changes if x["object_type"] == "DocumentSubmission"]
    for d in docs:
        assert c.actions.apply("record_document_submission", d, actor=staff).ok
    return ret, req, docs


# ================================================================ 감사 로그
def test_object_history_includes_changes_made_through_other_targets(tx):
    ret, req, docs = flow(tx)
    hist = AuditLog(tx).object_history("DocumentSubmission", docs[0])
    lines = [str(h) for h in hist]
    assert any(f"request_missing_documents [completed] agent-01(agent) ← TaxReturn({ret}) 대상" in s
               and "status 미요청→요청됨" in s for s in lines)
    assert any("record_document_submission [completed]" in s and "status 요청됨→제출완료" in s for s in lines)
    assert any("document_received [pending" in s for s in lines)


def test_trace_follows_execution(tx):
    ret, req, _ = flow(tx)
    t = AuditLog(tx).trace(req.execution_id)
    assert t["execution"]["actor"]["id"] == "agent-01" and t["execution"]["status"] == "completed"
    assert ("TaxReturn" not in {x["object_type"] for x in t["transitions"]})              # 신고 상태는 그대로
    assert {x["to_state"] for x in t["transitions"]} == {"요청됨", "진행중"}
    assert [e["event_type"] for e in t["events"]] == ["documents_requested"]


def test_verify_detects_tampering_deletion_and_drift(tx):
    ret, req, docs = flow(tx)
    audit = AuditLog(tx)
    assert audit.verify() == []
    tx.conn.execute("UPDATE minionto.action_log SET status = 'rejected' WHERE execution_id = %s",
                    (req.execution_id,))                                                   # 변조
    assert any(f.check == "chain" and "변조" in f.message for f in audit.verify())
    tx.conn.execute("UPDATE minionto.action_log SET status = 'completed' WHERE execution_id = %s",
                    (req.execution_id,))
    assert audit.verify() == []                                                            # 되돌리면 통과
    seq = tx.conn.execute("SELECT seq FROM minionto.action_log WHERE execution_id = %s",
                          (req.execution_id,)).fetchone()["seq"]
    tx.conn.execute("DELETE FROM minionto.state_transitions WHERE execution_id = %s", (req.execution_id,))
    assert any(f.check == "transitions" for f in audit.verify())                           # 전이 기록 누락
    tx.conn.execute("DELETE FROM minionto.action_log WHERE seq = %s", (seq,))               # 기록 삭제
    found = audit.verify()
    assert any(f.check == "chain" and "이어지지 않습니다" in f.message for f in found)
    tx.conn.execute("UPDATE document_submissions SET status = '미요청', submitted_at = NULL "
                    "WHERE submission_id = %s", (docs[-1],))                                 # 엔진 밖 변경
    assert any(f.check == "drift" and docs[-1] in f.message for f in audit.verify())


# ================================================================ 구독
def test_subscription_starts_review_when_all_documents_arrive(tx):
    ret, _, docs = flow(tx)
    runner = SubscriptionRunner(tx)
    handled = runner.run_once()                     # 자료가 모두 접수된 뒤에 돌았으므로 첫 이벤트에서 검토 시작
    assert [(h.status, h.result) for h in handled] == [("done", "completed")] + \
        [("skipped", "rejected")] * (len(docs) - 1)
    assert tx.get("TaxReturn", ret).status == "검토중"
    review = tx.conn.execute("SELECT actor FROM minionto.action_log WHERE action = 'start_review' "
                             "AND target_pk = %s", (ret,)).fetchall()
    assert [r["actor"]["id"] for r in review] == ["subscription:auto_start_review"]
    assert runner.run_once() == []                                                         # 다 처리함
    assert runner.replay("auto_start_review") == len(docs)
    again = runner.run_once()                                                              # 재처리
    assert all(h.status == "skipped" for h in again)                                       # 이미 검토중
    assert tx.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = 'start_review' "
                           "AND status = 'completed' AND target_pk = %s", (ret,)).fetchone()["n"] == 1


def test_subscription_definition_is_validated():
    raw = copy.deepcopy(RAW)
    raw["subscriptions"]["bad_target"] = {"description": "x", "event": "document_received",
                                         "target": "tax_return.customer", "action": "start_review"}
    raw["subscriptions"]["no_system"] = {"description": "x", "event": "documents_requested",
                                        "action": "complete_filing"}
    raw["subscriptions"]["no_event"] = {"description": "x", "event": "nothing_happened", "action": "start_review"}
    text = "\n".join(str(i) for i in validate(Ontology.model_validate(raw)).errors)
    assert "가면 Customer 인데 'start_review' 의 대상은 TaxReturn" in text
    assert "구독은 시스템 실행자" in text and "'complete_filing'" in text
    assert "이벤트 'nothing_happened' 를 내는 Action 이 없습니다" in text


def test_metrics(tx):
    flow(tx)
    SubscriptionRunner(tx).run_once()
    m = AuditLog(tx).metrics()
    acts = {(r["actor_type"], r["status"]): r["n"] for r in m["actions"]}
    assert acts[("agent", "completed")] >= 1 and acts[("system", "completed")] >= 1
    assert any(r["subscriber"] == "auto_start_review" for r in m["deliveries"])
