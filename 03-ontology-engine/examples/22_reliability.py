"""22장. 트랜잭션 경계 · 멱등성 · 오류 분류와 복구

    uv run python 03-ontology-engine/examples/22_reliability.py

1. 잠금 대기   — 다른 트랜잭션이 대상을 잡고 있을 때: 무한 대기 대신 시간 초과, 그리고 자동 재시도
2. 멱등 키     — 응답을 받지 못한 AI 의 재전송: 한 번만 실행하고 처음 응답을 돌려준다
3. 중복 방지   — 승인 대기 중인 같은 요청 (21장의 남은 문제)
4. 오류 분류   — 다시 하면 될 실패와 다시 해도 안 될 실패
5. 보상 Action — 커밋된 실행을 되돌리는 업무

모든 변경은 롤백한다. 1단계의 두 번째 연결도 커밋하지 않고 잠금만 잡았다가 놓는다.
"""

from __future__ import annotations

import copy
import sys
import threading
import time
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from minionto import Ontology, OntologyClient  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
REQ = "request_missing_documents"
TAX = {"id": "A001", "role": "세무사", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def line(label: str, res) -> None:
    extra = []
    if res.error_code:
        extra.append(f"code={res.error_code}")
    if res.status == "failed" or res.retryable:
        extra.append(f"retryable={res.retryable}")
    if res.attempts > 1:
        extra.append(f"attempts={res.attempts}")
    if res.replayed:
        extra.append("replayed")
    print(f"  {label:34s} [{res.status}] {res.messages[0].splitlines()[0]}" + (f"  ({', '.join(extra)})" if extra else ""))


def count(c, q, *args) -> int:
    return c.conn.execute(q, args).fetchone()["n"]


def pick(c, high_risk: bool) -> str:
    op = "eq" if high_risk else "ne"
    return c.actions.candidates(REQ, actor=AI).where(
        {"field": "customer.risk_level", "op": op, "value": "높음"}).order_by("return_id").first().pk


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    onto, other = OntologyClient.open(TAXOS, today=AS_OF), OntologyClient.open(TAXOS, today=AS_OF)
    a = onto.actions
    with onto, other, onto.conn.transaction(force_rollback=True):
        ret = pick(onto, high_risk=False)

        # ------------------------------------------------------------------ 1
        section("1. 잠금 대기 — 다른 트랜잭션이 같은 신고를 잡고 있다")
        a.lock_timeout, a.retry_backoff = "1s", 0.5
        other.conn.execute("BEGIN")
        other.conn.execute("SELECT 1 FROM tax_returns WHERE return_id = %s FOR UPDATE", (ret,))
        print(f"[연결 2] {ret} 행 잠금 (커밋하지 않고 붙잡고 있음), Action 의 잠금 대기 한도 {a.lock_timeout}")
        t0 = time.monotonic()
        line(f"AI비서 → {ret} 자료 요청", a.apply(REQ, ret, actor=AI))
        print(f"  (대기 {time.monotonic() - t0:.1f}초 후 실패. 18장의 측정 스크립트는 여기서 멈춰 있었다)")
        threading.Timer(1.2, lambda: other.conn.execute("ROLLBACK")).start()
        print("[연결 2] 1.2초 뒤 잠금을 놓도록 예약")
        t0 = time.monotonic()
        res = a.apply(REQ, ret, actor=AI, retries=3)
        line(f"AI비서 → {ret} 자료 요청 (retries=3)", res)
        print(f"  (총 {time.monotonic() - t0:.1f}초)")
        rows = onto.conn.execute("SELECT status, error FROM minionto.action_log WHERE target_pk = %s "
                                 "ORDER BY created_at", (ret,)).fetchall()
        print("  실행 기록:", ", ".join(f"{r['status']}{'(' + r['error']['code'] + ')' if r['error'] else ''}"
                                    for r in rows))

        # ------------------------------------------------------------------ 2
        section("2. 멱등 키 — 응답을 받지 못한 AI 가 같은 요청을 다시 보낸다")
        ret2 = pick(onto, high_risk=False)
        elsewhere = onto.actions.candidates(REQ, actor=AI).where(
            {"field": "return_id", "op": "ne", "value": ret2}).order_by("return_id").first().pk
        tasks = count(onto, "SELECT count(*) AS n FROM tasks")
        first = a.apply(REQ, ret2, actor=AI, idempotency_key="req-20270118-0001")
        print(f"  첫 요청은 실행됐지만 응답이 네트워크에서 사라졌다고 하자 (execution {first.execution_id[:8]})")
        line("같은 키로 재전송", (r1 := a.apply(REQ, ret2, actor=AI, idempotency_key="req-20270118-0001")))
        line("같은 키로 한 번 더", a.apply(REQ, ret2, actor=AI, idempotency_key="req-20270118-0001"))
        print(f"  재전송의 execution = {r1.execution_id[:8]} (처음과 같음), 업무 수 {tasks} → "
              f"{count(onto, 'SELECT count(*) AS n FROM tasks')} (한 번만 생성)")
        k = onto.conn.execute("SELECT replays, response->>'status' AS status FROM minionto.idempotency_keys "
                              "WHERE key = 'req-20270118-0001'").fetchone()
        print(f"  idempotency_keys: 저장된 응답 {k['status']}, 재생 {k['replays']}회")
        line(f"같은 키, 다른 신고({elsewhere})", a.apply(REQ, elsewhere, actor=AI, idempotency_key="req-20270118-0001"))
        line("키 없이 다시 (업무 규칙이 막음)", a.apply(REQ, ret2, actor=AI))

        # ------------------------------------------------------------------ 3
        section("3. 중복 방지 — 승인 대기 중인 같은 요청")
        risky = pick(onto, high_risk=True)
        x = a.apply(REQ, risky, actor=AI)
        y = a.apply(REQ, risky, actor=AI)
        print(f"  {risky} 첫 요청 → [{x.status}] approval {x.approval_id[:8]}, 이벤트 {x.events}")
        print(f"  {risky} 두 번째 → [{y.status}] approval {y.approval_id[:8]}, 이벤트 {y.events}")
        print("   ", y.messages[-1])
        print(f"  대기 중인 승인 요청: {count(onto, 'SELECT count(*) AS n FROM minionto.approval_requests WHERE target_pk = %s AND status = %s', risky, 'pending')}건 (21장에서는 2건)")

        # ------------------------------------------------------------------ 4
        section("4. 오류 분류 — 다시 하면 될 실패, 다시 해도 안 될 실패")
        raw = yaml.safe_load(TAXOS.read_text(encoding="utf-8"))
        raw = copy.deepcopy(raw)
        raw["object_types"]["DocumentSubmission"].pop("state_machine")
        raw["actions"]["broken_submit"] = {                     # 18-3 의 일부러 잘못 정의한 Action
            "display_name": "잘못 정의된 제출 처리", "target": "TaxReturn", "permission": {"roles": ["세무사"]},
            "edits": [{"update_linked": "submissions", "set": {"status": "제출완료", "submitted_at": None}}]}
        broken = OntologyClient(Ontology.model_validate(raw), base_dir=TAXOS.parent, today=AS_OF)
        broken._conn = onto.conn                                # 같은 연결·같은 롤백 트랜잭션
        t0 = time.monotonic()
        res = broken.actions.apply("broken_submit", ret, actor=TAX, retries=3)
        line("제출일 없이 제출완료 (DB 제약 위반)", res)
        print(f"  (retries=3 이었지만 시도 {res.attempts}회, {time.monotonic() - t0:.2f}초 — 같은 입력이면 같은 결과)")
        line("허용되지 않은 상태 전이", a.apply("complete_filing", ret, actor=TAX))

        # ------------------------------------------------------------------ 5
        section("5. 보상 Action — 커밋된 자료 요청을 되돌린다")
        done = a.apply(REQ, (ret3 := pick(onto, high_risk=False)), actor=AI)
        print(f"  AI비서가 {ret3} 에 자료 요청 → {len(done.changes)}건 변경")
        line("AI비서가 스스로 취소", a.apply("cancel_document_request", ret3, actor=AI, params={"reason": "x"}))
        undo = a.apply("cancel_document_request", ret3, actor=TAX, params={"reason": "다른 신고에 보낼 요청이었음"})
        print(undo)
        sub = next(c["pk"] for c in done.changes if c["object_type"] == "DocumentSubmission")
        print(f"\n  {sub} 상태 이력:")
        for h in a.history("DocumentSubmission", sub):
            print(f"    {h['from_state']} → {h['to_state']:6s} {h['action']:26s} {h['actor_id']}")
        print("  outbox:", [r["event_type"] for r in onto.conn.execute(
            "SELECT event_type FROM minionto.outbox WHERE target_pk = %s ORDER BY event_id", (ret3,)).fetchall()])
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
