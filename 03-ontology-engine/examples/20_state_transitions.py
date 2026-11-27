"""20장. Action 의 실행 조건과 상태 전이

    uv run python 03-ontology-engine/examples/20_state_transitions.py

1. 상태 머신     — 정의 파일에 선언한 신고(TaxReturn)의 상태와 전이
2. 지금 할 일    — AI 비서가 신고 하나에 대해 '지금 할 수 있는 Action'을 묻는다 (available)
3. 잘못된 전이   — 자료수집중인 신고를 바로 신고완료로 (차단)
4. 신고 업무     — 자료 요청 → 자료 접수 → 검토 시작 → 보완 요청 → 재검토 → 신고 완료
5. 상태 이력     — 엔진이 남긴 상태 전이 기록
6. 동시 변경     — 다른 트랜잭션이 먼저 바꾼 자료를 덮어쓰지 않는다

모든 변경은 롤백한다. 6단계만 두 번째 연결로 실제 커밋을 한 뒤, 끝나면 원래 값으로 되돌린다.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from minionto import OntologyClient  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"

TAX = {"id": "A001", "role": "세무사", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show(res) -> None:
    print(res)
    for t in res.transitions:
        print(f"  ↪ {t['object_type']}({t['pk']}) {t['property']}: {t['from'] or '(생성)'} → {t['to']}")


def pick_return(onto: OntologyClient) -> str:
    """AI 가 자료를 요청할 수 있고 승인이 필요 없는(고위험이 아닌) 첫 신고"""
    return (onto.actions.candidates("request_missing_documents", actor=AI)
            .where({"field": "customer.risk_level", "op": "ne", "value": "높음"})
            .order_by("return_id").first().pk)


def count(conn, query: str, *args) -> int:
    return conn.execute(query, args).fetchone()["n"]


def scenario(onto: OntologyClient) -> str:
    a = onto.actions
    a.ensure_tables()
    logged = count(onto.conn, "SELECT count(*) AS n FROM minionto.state_transitions")
    section("1. 상태 머신 — TaxReturn.status")
    m = a.states.machine("TaxReturn")
    print(f"처음: {', '.join(m.initial)}  ·  종료: {', '.join(m.terminal)}")
    for action, t in m.transitions.items():
        print(f"  {'·'.join(t.from_):10s} → {t.to:8s} {action}")

    ret = pick_return(onto)
    staff = {"id": onto.get("TaxReturn", ret).customer.accountant_id, "role": "실무자", "type": "human"}

    section(f"2. 지금 할 수 있는 일 — AI비서가 {ret} 에 대해 묻는다")
    print(a.available("TaxReturn", ret, actor=AI))

    section("3. 허용되지 않은 전이 — 자료수집중인 신고를 바로 완료 처리")
    show(a.apply("complete_filing", ret, actor=TAX))

    section("4. 신고 업무를 끝까지")
    show(a.apply("request_missing_documents", ret, actor=AI))
    show(a.apply("start_review", ret, actor=AI))                       # 자료가 아직 안 옴
    for s in onto.get("TaxReturn", ret).submissions.where(status=["미요청", "요청됨"]).order_by("submission_id"):
        show(a.apply("record_document_submission", s, actor=staff))     # 고객이 자료를 보냄
    show(a.apply("start_review", ret, actor=AI))

    print(f"\n세무사가 본 {ret} (검토중):")
    print(a.available("TaxReturn", ret, actor=TAX))

    show(a.apply("return_to_collection", ret, actor=TAX, params={"reason": "카드 매출 자료 재확인 필요"}))
    show(a.apply("start_review", ret, actor=AI))
    print("\n읽은 뒤 상태가 바뀐 경우 (expect):")
    show(a.apply("complete_filing", ret, actor=TAX, expect={"status": "자료수집중"}))
    show(a.apply("complete_filing", ret, actor=TAX, expect={"status": "검토중"}))

    print(f"\n완료 후 {ret}:")
    print(a.available("TaxReturn", ret, actor=TAX))

    section(f"5. 상태 이력 — {ret}")
    for h in a.history("TaxReturn", ret):
        print(f"  {h['from_state'] or '(생성)':8s} → {h['to_state']:8s} {h['action']:22s} {h['actor_id']} ({h['actor_type']})")
    n = count(onto.conn, "SELECT count(*) AS n FROM minionto.state_transitions") - logged
    print(f"  (이 시나리오에서 남은 상태 전이 기록: 신고·자료·업무 합계 {n}건)")
    return ret


def concurrent_change() -> None:
    section("6. 동시 변경 — 다른 트랜잭션이 먼저 바꾼 자료")
    c1, c2 = OntologyClient.open(TAXOS, today=AS_OF), OntologyClient.open(TAXOS, today=AS_OF)
    ret = pick_return(c1)
    sub = c1.get("TaxReturn", ret).submissions.where(status="미요청").order_by("submission_id").first()
    original = c2.conn.execute("SELECT status, submitted_at FROM document_submissions WHERE submission_id = %s",
                               (sub.pk,)).fetchone()
    out = {}
    q_tasks = "SELECT count(*) AS n FROM tasks WHERE return_id = %s"
    try:
        c2.conn.execute("BEGIN")
        c2.conn.execute("UPDATE document_submissions SET status = '제출완료', submitted_at = %s "
                        "WHERE submission_id = %s", (AS_OF, sub.pk))
        print(f"[연결 2] {sub.pk} 를 제출완료로 바꿈 (아직 커밋 전, 행 잠금 보유)")
        with c1.conn.transaction(force_rollback=True):
            before = count(c1.conn, q_tasks, ret)
            th = threading.Thread(target=lambda: out.update(
                res=c1.actions.apply("request_missing_documents", ret, actor=AI)))
            th.start()
            time.sleep(1.0)
            print(f"[연결 1] AI비서의 {ret} 자료 요청 — 자료 행의 잠금을 기다리는 중")
            c2.conn.execute("COMMIT")
            print("[연결 2] 커밋")
            th.join(10)
            show(out["res"])
            print(f"  {ret} 의 업무 수: 실행 전 {before}건 → 실행 후 {count(c1.conn, q_tasks, ret)}건 (전체 롤백)")
    finally:
        c2.conn.execute("UPDATE document_submissions SET status = %s, submitted_at = %s WHERE submission_id = %s",
                        (original["status"], original["submitted_at"], sub.pk))
        print(f"(정리: {sub.pk} 를 원래 값 '{original['status']}' 로 되돌림)")
        c1.close()
        c2.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with OntologyClient.open(TAXOS, today=AS_OF) as onto:
        with onto.conn.transaction(force_rollback=True):
            scenario(onto)
    concurrent_change()
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
