"""23장. 외부 시스템 연동 — 트랜잭셔널 아웃박스와 발송 작업자

    uv run python 03-ontology-engine/examples/23_outbox_notifications.py

1. 직접 호출의 두 사고 — Action 안에서 보내면 '보냈는데 DB 는 롤백', 커밋 뒤에 보내면 '커밋했는데 못 보냄'
2. 아웃박스          — Action 은 이벤트만 남기고, 작업자가 보낸 뒤 결과를 Action 으로 기록
3. 일시 장애와 순서  — 재시도(지수 백오프), 같은 신고의 요청·취소는 순서대로
4. 작업자 중단       — 보낸 직후 죽어도, 다시 보낼 때 외부 서비스가 멱등 키로 중복을 거른다
5. 영구 실패         — 포기(dead) → 담당자에게 직접 연락 업무 → 원인 해결 후 다시 보내기

외부 발송 서비스는 같은 프로세스의 모의 서비스(mock_notifier.py)다. 모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "examples"))

warnings.filterwarnings("ignore", message="Using `httpx` with")
from fastapi.testclient import TestClient  # noqa: E402
from mock_notifier import create_mock_notifier  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.outbox import SimulatedCrash, outbox_rows, requeue  # noqa: E402

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


def show(outcomes) -> None:
    for o in outcomes:
        print("  ", o)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app = create_mock_notifier()
    box = app.state.box
    notifier = integrations.Notifier(TestClient(app))
    with OntologyClient.open(TAXOS, today=AS_OF) as onto, onto.conn.transaction(force_rollback=True):
        a = onto.actions
        rets = [r.pk for r in a.candidates(REQ, actor=AI).where(
            {"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").limit(4)]
        risky = a.candidates(REQ, actor=AI).where(
            {"field": "customer.risk_level", "op": "eq", "value": "높음"}).order_by("return_id").first().pk
        phone = onto.get("TaxReturn", rets[0]).customer.phone

        # ------------------------------------------------------------------ 1
        section("1. 직접 호출의 두 사고")
        tasks = onto.conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"]
        try:
            with onto.conn.transaction():                       # (가) 트랜잭션 안에서 보내기
                notifier.send("direct-1", phone, "문자", "[세무사무소] 자료를 보내 주세요")
                onto.conn.execute("INSERT INTO tasks (task_id) VALUES ('T99999')")   # 이어지는 DB 작업 실패
        except Exception as e:  # noqa: BLE001
            print(f"(가) 보낸 뒤 DB 작업 실패 → 롤백: {type(e).__name__}")
        print(f"    외부 메시지 {len(box['messages'])}건, 업무 {tasks} → "
              f"{onto.conn.execute('SELECT count(*) AS n FROM tasks').fetchone()['n']}건  ← 보냈는데 기록이 없다")
        print("(나) 커밋 뒤에 보내기: 커밋과 발송 사이에 프로세스가 죽으면 기록은 있는데 메시지는 없다")
        box["messages"].clear()

        # ------------------------------------------------------------------ 2
        section("2. 아웃박스 — Action 은 이벤트만, 작업자가 보낸다")
        print(a.apply(REQ, rets[0], actor=AI).messages[0], f"— {rets[0]} 자료 요청 (AI비서)")
        print(a.apply(REQ, risky, actor=AI).status, f"— {risky} 승인 요청 (AI비서)")
        print("\n보내기 전 outbox:")
        for r in outbox_rows(onto, "pending"):
            print(f"   #{r['event_id']:<4d} {r['event_type']:20s} {r['target_pk']}  {r['status']}")
        d = integrations.make_dispatcher(onto, notifier, backoff=0.2)
        print("\n작업자 실행:")
        show(d.run_until_idle())
        print("\n모의 발송 서비스가 받은 메시지:")
        for m in box["messages"].values():
            print(f"   {m['message_id']} {m['channel']:3s} {m['to']:16s} {m['text']}")
        print("\nNotification 객체 (발송 결과를 온톨로지에 기록):")
        for n in onto.objects("Notification").order_by("notification_id"):
            print(f"   {n.notification_id} event #{n.event_id} {n.event_type:20s} {n.recipient}/{n.channel} "
                  f"{n.status} {n.external_id} 시도 {n.attempts}")

        # ------------------------------------------------------------------ 3
        section("3. 일시 장애와 순서 — 같은 신고의 요청과 취소")
        a.apply(REQ, rets[1], actor=AI)
        a.apply("cancel_document_request", rets[1], actor=TAX, params={"reason": "다른 신고에 보낼 요청이었음"})
        box["fail_next"] = 2
        print(f"{rets[1]}: 자료 요청 → 바로 취소. 발송 서비스는 다음 두 번 503")
        show(d.run_until_idle())
        for m in list(box["messages"].values())[-2:]:
            print(f"   {m['message_id']} {m['text']}")

        # ------------------------------------------------------------------ 4
        section("4. 작업자 중단 — 보낸 직후, 기록하기 전에 죽으면")
        a.apply(REQ, rets[2], actor=AI)
        calls, sent = box["calls"], len(box["messages"])
        d.crash_after_send = True
        try:
            d.run_once()
        except SimulatedCrash as e:
            print(f"   중단: {e}")
        pending = [r for r in outbox_rows(onto, "pending")]
        print(f"   outbox 대기 {len(pending)}건 (#{pending[0]['event_id']}), 이 단계에서 나간 외부 메시지 "
              f"{len(box['messages']) - sent}건")
        d.crash_after_send = False
        print("   작업자 재시작:")
        show(d.run_until_idle())
        print(f"   이 단계의 외부 호출 {box['calls'] - calls}회, 외부 메시지 {len(box['messages']) - sent}건, "
              f"{rets[2]} 의 Notification {onto.objects('Notification').where(return_id=rets[2]).count()}건")

        # ------------------------------------------------------------------ 5
        section("5. 영구 실패 — 포기하고 사람에게 넘기기")
        bad = onto.get("TaxReturn", rets[3]).customer.phone
        box["reject"].add(bad)
        a.apply(REQ, rets[3], actor=AI)
        out = d.run_until_idle()
        show(out)
        n = onto.objects("Notification").where(return_id=rets[3]).one()
        print(f"   Notification {n.notification_id}: {n.status} — {n.detail}")
        t = onto.objects("Task").where(return_id=rets[3], task_type="고객연락").one()
        print(f"   Task {t.task_id}: {t.task_type} {t.status} 담당 {t.assignee_id} (created_by {t.created_by})")
        box["reject"].clear()
        print(f"   번호를 확인해 고친 뒤 다시 보내기 → requeue #{out[0].event_id}: {requeue(onto, out[0].event_id)}")
        show(d.run_until_idle())
        for n in onto.objects("Notification").where(return_id=rets[3]).order_by("notification_id"):
            print(f"   {n.notification_id} event #{n.event_id} {n.status} {n.external_id or '-'}")

        print("\noutbox 최종 상태:", ", ".join(
            f"{r['status']} {r['n']}" for r in onto.conn.execute(
                "SELECT status, count(*) AS n FROM minionto.outbox GROUP BY 1 ORDER BY 1").fetchall()))
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
