"""24장. 감사 로그 · 이벤트 구독 · 피드백 루프

    uv run python 03-ontology-engine/examples/24_audit_feedback.py

1. 자료 요청 이후  — AI 요청 → 알림 발송 → 자료 접수 → (구독) 검토 시작 → 신고 완료
2. 이력과 추적     — 신고·자료의 객체별 이력, AI 요청 실행 하나의 추적
3. 완전성 점검     — 기록 변조, 엔진 밖 변경을 찾아낸다 / 구독 재처리
4. 피드백 루프     — 가설은 데이터로 먼저 확인 / Function 과 사람 판단의 불일치를 검토 업무로
5. 운영 지표

모든 DB 변경은 롤백한다. 외부 발송은 23장의 모의 서비스를 쓴다.
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from collections import Counter
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "examples"))

warnings.filterwarnings("ignore", message="Using `httpx` with")
from fastapi.testclient import TestClient  # noqa: E402
from mock_notifier import create_mock_notifier  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.audit import AuditLog  # noqa: E402
from minionto.subscriptions import SubscriptionRunner  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
TAX = {"id": "A001", "role": "세무사", "type": "human"}

spec = importlib.util.spec_from_file_location("taxos_integrations", ENGINE / "ontologies" / "taxos" / "integrations.py")
integrations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrations)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with OntologyClient.open(TAXOS, today=AS_OF) as onto, onto.conn.transaction(force_rollback=True):
        a, audit, subs = onto.actions, AuditLog(onto), SubscriptionRunner(onto)
        dispatcher = integrations.make_dispatcher(onto, integrations.Notifier(TestClient(create_mock_notifier())))

        # ------------------------------------------------------------------ 1
        section("1. 자료 요청 이후")
        ret = a.candidates(REQ, actor=AI).where(
            {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").first().pk
        staff = {"id": onto.get("TaxReturn", ret).customer.accountant_id, "role": "실무자", "type": "human"}
        req = a.apply(REQ, ret, actor=AI)
        print(f"AI비서 {ret} 자료 요청 → {req.status}")
        for o in dispatcher.run_until_idle():
            print("  발송:", o)
        docs = [c["pk"] for c in req.changes if c["object_type"] == "DocumentSubmission"]
        for d in docs:
            print(f"실무자 {staff['id']} {d} 접수 → {a.apply('record_document_submission', d, actor=staff).status}")
            for h in subs.run_once():
                print("  구독:", h)
        print(f"{ret} 상태: {onto.get('TaxReturn', ret).status}")
        print(f"세무사 신고 완료 → {a.apply('complete_filing', ret, actor=TAX).status}")

        # ------------------------------------------------------------------ 2
        section(f"2. 이력과 추적 — {ret}")
        print(f"TaxReturn({ret}) 이력:")
        for e in audit.object_history("TaxReturn", ret):
            print("  ", e)
        print(f"\nDocumentSubmission({docs[0]}) 이력:")
        for e in audit.object_history("DocumentSubmission", docs[0]):
            print("  ", e)
        t = audit.trace(req.execution_id)
        print(f"\nAI 요청 실행 {req.execution_id[:8]} 추적:")
        print(f"   실행자 {t['execution']['actor']['id']} ({t['execution']['actor']['type']}) → {t['execution']['status']}")
        print("   상태 전이:", ", ".join(f"{x['object_type']}({x['target_pk']}) {x['from_state']}→{x['to_state']}"
                                    for x in t["transitions"]))
        for ev in t["events"]:
            n = ev["notifications"][0] if ev["notifications"] else {}
            print(f"   이벤트 #{ev['event_id']} {ev['event_type']} [{ev['status']}] → 알림 "
                  f"{n.get('notification_id')} {n.get('status')} {n.get('external_id')}")

        # ------------------------------------------------------------------ 3
        section("3. 완전성 점검과 재처리")
        print("점검:", audit.verify() or "이상 없음")
        onto.conn.execute("UPDATE minionto.action_log SET messages = '[\"0건 변경\"]' WHERE execution_id = %s",
                          (req.execution_id,))
        print("실행 기록 한 줄의 메시지를 SQL 로 고친 뒤:")
        for f in audit.verify():
            print("  ", f)
        onto.conn.execute("UPDATE minionto.action_log SET messages = '[\"3건 변경\"]' WHERE execution_id = %s",
                          (req.execution_id,))
        print("원래대로 되돌린 뒤:", audit.verify() or "이상 없음")
        onto.conn.execute("UPDATE customers SET risk_level = '높음' WHERE customer_id = 'C0001'")
        onto.conn.execute("UPDATE document_submissions SET status = '미요청', submitted_at = NULL "
                          "WHERE submission_id = %s", (docs[0],))
        print("고객 C0001 위험 등급과 자료 하나의 상태를 SQL 로 직접 바꾼 뒤:")
        for f in audit.verify():
            print("  ", f)
        print("   (C0001 은 엔진이 한 번도 바꾼 적이 없어 대조할 기록이 없으므로 찾지 못한다)")
        onto.conn.execute("UPDATE document_submissions SET status = '제출완료', submitted_at = %s "
                          "WHERE submission_id = %s", (AS_OF, docs[0]))
        print(f"\n구독 재처리: 처리 기록 {subs.replay('auto_start_review')}건 삭제 후 다시 실행")
        for h in subs.run_once():
            print("  구독:", h)
        n = onto.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = 'start_review' "
                              "AND status = 'completed' AND target_pk = %s", (ret,)).fetchone()["n"]
        print(f"   {ret} 의 검토 시작 실행: {n}건")

        # ------------------------------------------------------------------ 4
        section("4. 피드백 루프")
        print("(가) 가설: 자료 제출이 늦은 고객일수록 기한후신고가 많다 → 신고 지연 위험도에 반영할까?")
        for r in onto.conn.execute(
                """WITH lag AS (SELECT r.customer_id, avg(s.submitted_at - s.requested_at) d
                                FROM document_submissions s JOIN tax_returns r USING (return_id)
                                WHERE s.status = '제출완료' AND s.requested_at IS NOT NULL GROUP BY 1),
                        late AS (SELECT customer_id, bool_or(status = '기한후신고') had FROM tax_returns GROUP BY 1)
                   SELECT had, count(*) AS customers, round(avg(d), 1) AS avg_days
                   FROM lag JOIN late USING (customer_id) GROUP BY 1 ORDER BY 1""").fetchall():
            print(f"   기한후신고 이력 {'있음' if r['had'] else '없음'}: 고객 {r['customers']}명, "
                  f"요청~제출 평균 {r['avg_days']}일")
        print("   → 차이가 없다. Function 을 바꾸지 않는다.")

        print("\n(나) Function(신고 지연 위험도)과 사람이 정한 위험 등급 비교 — 자료수집중 신고가 있는 고객")
        table, mismatch = Counter(), []
        for c in onto.objects("TaxReturn").where(status="자료수집중").pivot("customer").order_by("customer_id"):
            r = onto.functions.call("filing_delay_risk", customer=c)
            table[(r["level"], c.risk_level)] += 1
            if r["level"] == "높음" and c.risk_level != "높음":
                mismatch.append((c.pk, r["score"], c.risk_level, r["reasons"]))
        levels = ["낮음", "보통", "높음"]
        print("   Function \\ 사람  " + "  ".join(f"{x:>4s}" for x in levels))
        for f in levels:
            print(f"   {f:15s}" + "  ".join(f"{table[(f, h)]:>5d}" for h in levels))
        print(f"   Function 은 높음, 사람은 높음이 아닌 고객 {len(mismatch)}명 → 세무사 검토 대상")
        for pk, score, human, reasons in sorted(mismatch, key=lambda x: -x[1])[:3]:
            print(f"     {pk} 점수 {score} (사람 {human}) — {', '.join(reasons)}")

        pick = None
        for pk, score, human, reasons in sorted(mismatch, key=lambda x: (x[2] != "낮음", -x[1])):
            r = a.candidates(REQ, actor=AI).where(customer_id=pk).first()
            if r:
                pick = (pk, score, human, r.pk)
                break
        pk, score, human, rpk = pick
        before = a.check(REQ, rpk, actor=AI)
        upd = a.apply("update_risk_level", pk, actor=TAX,
                      params={"level": "높음", "reason": f"피드백 검토: 신고 지연 위험도 {score}점"})
        after = a.check(REQ, rpk, actor=AI)
        print(f"\n   세무사가 {pk} 검토 → 위험 등급 {human}→높음 ({upd.status})")
        print(f"   AI비서의 {rpk} 자료 요청 판단: {before.status} → {after.status}")

        # ------------------------------------------------------------------ 5
        section("5. 운영 지표")
        m = audit.metrics()
        print("실행:", ", ".join(f"{r['actor_type']}/{r['status']} {r['n']}" for r in m["actions"]))
        print("outbox:", ", ".join(f"{r['status']} {r['n']}" for r in m["outbox"]))
        print("구독:", ", ".join(f"{r['subscriber']} {r['status']}/{r['result']} {r['n']}" for r in m["deliveries"]))
        print("정책 판단:", ", ".join(f"{r['decision']}/{r['kind']} {r['n']}" for r in m["policy"]) or "-")
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
