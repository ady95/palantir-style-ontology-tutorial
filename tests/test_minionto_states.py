"""20장. 상태 머신 · 상태 전이 강제 · 동시 변경 감지 테스트

Action 테스트와 같이 바깥 트랜잭션 안에서 실행하고 롤백한다. 동시 변경 테스트만 두 번째 연결로
실제 커밋을 하고, 끝나면 원래 값으로 되돌린다. DB에 연결할 수 없으면 건너뛴다.
실행: uv run pytest -q tests/test_minionto_states.py
"""

from __future__ import annotations

import copy
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parent.parent / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))

from minionto import Ontology, OntologyClient  # noqa: E402
from minionto.validator import validate  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
RAW = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))

TAX = {"id": "A001", "role": "세무사", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def _open(onto=None):
    c = OntologyClient(onto, base_dir=TAXOS.parent, today=AS_OF) if onto else \
        OntologyClient.open(TAXOS, today=AS_OF)
    try:
        c.conn
    except Exception:  # noqa: BLE001
        pytest.skip("실습 DB에 연결할 수 없습니다")
    return c


@pytest.fixture
def tx():
    c = _open()
    with c.conn.transaction(force_rollback=True):
        yield c
    c.close()


def scalar(c, q, *args):
    return next(iter(c.conn.execute(q, args).fetchone().values()))


def issues(raw) -> list[str]:
    return [str(i) for i in validate(Ontology.model_validate(raw), base_dir=TAXOS.parent).issues]


def staff_of(c, ret) -> dict:
    return {"id": c.get("TaxReturn", ret).customer.accountant_id, "role": "실무자", "type": "human"}


# ================================================================ 정의 검증
def test_taxos_state_machines_are_consistent():
    assert issues(RAW) == []


def test_validator_catches_mismatches():
    raw = copy.deepcopy(RAW)
    tr = raw["object_types"]["TaxReturn"]["state_machine"]["transitions"]
    del tr["complete_filing"]                                         # Action 은 신고완료로 바꾸는데 전이가 없음
    tr["start_review"]["to"] = "신고완료"                              # 선언과 edits 가 다름
    tr["reopen"] = {"from": "신고완료", "to": "검토중"}                  # 종료 상태에서 나감 + 없는 Action
    raw["actions"]["start_review"]["edits"][1]["where"] = {"field": "task_type", "op": "eq", "value": "자료요청"}
    raw["actions"]["return_to_collection"]["edits"][2]["values"]["status"] = "취소"
    raw["actions"]["file_late"]["preconditions"].append(
        {"message": "검토중만", "check": {"field": "status", "op": "eq", "value": "검토중"}})
    text = "\n".join(issues(raw))
    assert "상태 머신에 'complete_filing' 전이가 없습니다" in text
    assert "상태 머신은 'start_review' → 신고완료 인데 '검토중' 로 바꿉니다" in text
    assert "종료 상태 '신고완료' 에서 나가는 전이입니다" in text
    assert "Action 'reopen' 이 없습니다" in text
    assert "시작 상태를 where 의 status 조건" in text                  # update_linked 의 where
    assert "initial(대기, 진행중, 완료)에 없습니다" in text               # 취소로 새 업무 생성
    assert "상태 머신이 이미 검사하는 조건입니다" in text                 # 중복 실행 조건 (경고)


# ================================================================ 실행 전 검사
def test_state_is_checked_with_other_rules(tx):
    ret = tx.objects("TaxReturn").where(status="자료수집중").where(
        {"some": {"link": "submissions", "where": {"field": "status", "op": "eq", "value": "미요청"}}}
    ).order_by("return_id").first()
    res = tx.actions.apply("complete_filing", ret, actor=TAX)
    assert res.status == "rejected"
    assert res.messages[0].startswith("현재 상태가 '자료수집중' 입니다")     # 상태 사유와
    assert "아직 제출되지 않은 자료가 있습니다." in res.messages              # 다른 사유를 함께


def test_candidates_include_state(tx):
    q = tx.actions.candidates("start_review")
    expected = scalar(tx, """SELECT count(*) FROM tax_returns r WHERE status = '자료수집중' AND NOT EXISTS (
        SELECT 1 FROM document_submissions s WHERE s.return_id = r.return_id AND s.status IN ('미요청','요청됨'))""")
    assert q.count() == expected
    assert all(r.status == "검토중" for r in tx.actions.candidates("complete_filing").limit(50))


# ================================================================ 신고 흐름
def test_filing_flow_and_history(tx):
    ret = tx.actions.candidates("request_missing_documents", actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk
    a = tx.actions
    assert a.apply("request_missing_documents", ret, actor=AI).ok
    assert a.apply("start_review", ret, actor=AI).status == "rejected"          # 자료가 아직 안 옴
    staff = staff_of(tx, ret)
    for s in tx.get("TaxReturn", ret).submissions.where(status=["미요청", "요청됨"]).all():
        assert a.apply("record_document_submission", s, actor=staff).ok
    review = a.apply("start_review", ret, actor=AI)
    assert review.ok
    moved = {(t["object_type"], t["from"], t["to"]) for t in review.transitions}
    assert ("TaxReturn", "자료수집중", "검토중") in moved
    assert ("Task", "진행중", "완료") in moved and ("Task", None, "대기") in moved
    done = a.apply("complete_filing", ret, actor=TAX)
    assert done.ok and tx.get("TaxReturn", ret).status == "신고완료"
    hist = [(h["from_state"], h["to_state"], h["action"]) for h in a.history("TaxReturn", ret)]
    assert hist == [("자료수집중", "검토중", "start_review"), ("검토중", "신고완료", "complete_filing")]

    av = a.available("TaxReturn", ret, actor=TAX)                              # 종료 상태
    assert av.state["terminal"] and av.state["next"] == {}
    assert not any(c.ok for c in av.actions)


def test_backward_transition_is_explicit(tx):
    ret = tx.objects("TaxReturn").where(status="검토중").order_by("return_id").first()
    assert tx.actions.apply("return_to_collection", ret, actor=staff_of(tx, ret.pk),
                            params={"reason": "x"}).status == "denied"            # 세무사만
    res = tx.actions.apply("return_to_collection", ret, actor=TAX, params={"reason": "영수증 누락"})
    assert res.ok and tx.get("TaxReturn", ret.pk).status == "자료수집중"
    assert {("Task", "취소")} <= {(t["object_type"], t["to"]) for t in res.transitions}


# ================================================================ 변경 시점의 강제
def test_undeclared_transition_is_rejected_and_rolled_back():
    raw = copy.deepcopy(RAW)
    raw["actions"]["broken_submit"] = {                                     # 상태 머신에 없는 전이
        "display_name": "잘못 정의된 제출 처리", "target": "TaxReturn", "permission": {"roles": ["세무사"]},
        "edits": [{"create": "Task", "values": {"customer_id": "$target.customer_id", "task_type": "고객연락",
                                                "assignee_id": "A001", "status": "대기", "due_date": "$today",
                                                "created_by": "$actor.type", "created_at": "$now"}},
                  {"update_linked": "submissions", "set": {"status": "제출완료", "submitted_at": "$today"}}]}
    c = _open(Ontology.model_validate(raw))
    try:
        with c.conn.transaction(force_rollback=True):
            ret = c.objects("TaxReturn").where(status="자료수집중").order_by("return_id").first()
            tasks = scalar(c, "SELECT count(*) FROM tasks")
            res = c.actions.apply("broken_submit", ret, actor=TAX)
            assert res.status == "rejected" and res.messages[0].startswith("허용되지 않은 상태 전이")
            assert scalar(c, "SELECT count(*) FROM tasks") == tasks           # 앞의 create 도 롤백
    finally:
        c.close()


def test_expect_detects_stale_read(tx):
    ret = tx.objects("TaxReturn").where(status="검토중").order_by("return_id").first()
    res = tx.actions.apply("complete_filing", ret, actor=TAX, expect={"status": "자료수집중"})
    assert res.status == "rejected" and "예상 '자료수집중', 현재 '검토중'" in res.messages[0]
    assert tx.actions.apply("complete_filing", ret, actor=TAX, expect={"status": "검토중"}).ok


def test_concurrent_change_is_detected():
    """링크 객체(자료)를 다른 트랜잭션이 먼저 바꾸면, 변경 직전의 재확인에서 잡는다"""
    c1, c2 = _open(), _open()
    ret = c1.actions.candidates("request_missing_documents", actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk
    sub = c1.get("TaxReturn", ret).submissions.where(status="미요청").order_by("submission_id").first()
    original = c2.conn.execute("SELECT status, submitted_at FROM document_submissions WHERE submission_id=%s",
                               (sub.pk,)).fetchone()
    result = {}
    try:
        c2.conn.execute("BEGIN")                                                  # c2 는 autocommit 연결
        c2.conn.execute("UPDATE document_submissions SET status='제출완료', submitted_at=%s "
                        "WHERE submission_id=%s", (AS_OF, sub.pk))              # 잠금을 쥔 채 아직 커밋 전
        with c1.conn.transaction(force_rollback=True):
            th = threading.Thread(target=lambda: result.update(
                res=c1.actions.apply("request_missing_documents", ret, actor=AI)))
            th.start()
            time.sleep(1.0)                                                       # c1 이 행 잠금을 기다리는 동안
            c2.conn.execute("COMMIT")
            th.join(10)
        res = result["res"]
        assert res.status == "rejected"
        assert res.messages[0].startswith(f"동시 변경 감지: DocumentSubmission({sub.pk}) 의 status")
    finally:
        c2.conn.execute("ROLLBACK")                                               # 실패했을 때를 위한 정리
        c2.conn.execute("UPDATE document_submissions SET status=%s, submitted_at=%s WHERE submission_id=%s",
                        (original["status"], original["submitted_at"], sub.pk))
        c1.close()
        c2.close()


# ================================================================ check · available
def test_check_reports_missing_input_without_writing(tx):
    logs = scalar(tx, "SELECT count(*) FROM minionto.action_log")
    ret = tx.objects("TaxReturn").where(status="검토중").order_by("return_id").first()
    chk = tx.actions.check("return_to_collection", ret, actor=TAX)
    assert chk.status == "needs_input" and chk.missing == ["reason"] and chk.next_state == "자료수집중"
    assert tx.actions.check("return_to_collection", ret, actor=TAX, params={"reason": "x"}).status == "ready"
    assert tx.actions.check("return_to_collection", ret, actor=AI).status == "denied"
    av = tx.actions.available("TaxReturn", ret.pk, actor=TAX)
    assert av.state == {"property": "status", "value": "검토중", "terminal": False,
                        "next": {"return_to_collection": "자료수집중", "complete_filing": "신고완료",
                                 "file_late": "기한후신고"}}
    assert {c.action: c.status for c in av.actions}["complete_filing"] == "ready"
    assert scalar(tx, "SELECT count(*) FROM minionto.action_log") == logs          # 아무것도 기록하지 않음
