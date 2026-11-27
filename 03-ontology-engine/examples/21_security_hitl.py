"""21장. 인증 · 권한 정책 · Human-in-the-loop 승인 워크플로

    uv run python 03-ontology-engine/examples/21_security_hitl.py

1. 인증          — 토큰(JWT) 발급, 토큰 없음·만료·위조·범위 부족
2. 역할별 조회   — 같은 요청을 세무사·실무자·AI비서 토큰으로 (행·속성 정책)
3. 정책 위반     — 감춘 속성으로 검색, 범위 밖 객체에 Action·Function
4. 승인 워크플로 — 미리보기·기한·알림 이벤트 / 정상 승인 / 미리보기와 달라진 승인 / 기한 만료
5. 판단 기록     — 정책 판단 기록과 알림 이벤트

API 는 FastAPI TestClient 로 부르고(포트 불필요), 모든 변경은 롤백한다.
토큰 서명 키는 .env 의 MINIONTO_TOKEN_SECRET 를 쓰고, 없으면 실습용 고정 키를 쓴다.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import warnings
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

warnings.filterwarnings("ignore", message="Using `httpx` with")
from dotenv import load_dotenv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from minionto.api import Served, create_app  # noqa: E402
from minionto.auth import JwtAuth  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
P = "/v1/ontologies/taxos"
REQ = "request_missing_documents"
DEMO_SECRET = "minionto-demo-secret-do-not-use-in-production"

TAX = {"id": "A001", "role": "세무사", "type": "human"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}
ALL = ["objects:read", "functions:call", "actions:apply", "approvals:decide"]
WORK = ["objects:read", "functions:call", "actions:apply"]


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def short(resp) -> str:
    b = resp.json()
    if isinstance(b, dict) and "status" in b and "messages" in b:
        return f"HTTP {resp.status_code} {b['status']} — " + " / ".join(b["messages"])
    return f"HTTP {resp.status_code} — {b.get('detail', b) if isinstance(b, dict) else b}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ENGINE.parent / ".env")
    secret = os.getenv("MINIONTO_TOKEN_SECRET") or DEMO_SECRET
    auth = JwtAuth(secret)
    served = Served.from_file(TAXOS)
    engine = served.client(today=AS_OF)

    def token(actor, scopes, ttl=timedelta(hours=1)):
        tok = auth.issue(sub=actor["id"], role=actor["role"], actor_type=actor["type"], scopes=scopes, ttl=ttl)
        return {"Authorization": f"Bearer {tok}"}

    with engine, engine.conn.transaction(force_rollback=True):
        engine.actions.ensure_tables()
        start = engine.conn.execute("SELECT coalesce(max(decision_id), 0) AS n FROM minionto.policy_decisions"
                                    ).fetchone()["n"]
        app = create_app([served], auth=auth, today=AS_OF, connect=lambda s: nullcontext(engine))
        http = TestClient(app)
        tax, staff, ai = token(TAX, ALL), token(STAFF, WORK), token(AI, WORK)

        # ------------------------------------------------------------------ 1
        section("1. 인증 — 토큰이 실행자와 범위를 정한다")
        raw = ai["Authorization"].split()[1]
        payload = json.loads(base64.urlsafe_b64decode(raw.split(".")[1] + "=="))
        shown = {k: payload[k] for k in ("iss", "aud", "sub", "role", "actor_type", "scope")}
        print(f"AI비서 토큰 ({len(raw)}자, 점으로 나뉜 세 부분) — 본문:")
        print("  ", json.dumps(shown, ensure_ascii=False))
        print(f"   exp - iat = {payload['exp'] - payload['iat']}초, jti = {payload['jti'][:8]}…")
        print("토큰 없음         →", short(http.get(P)))
        print("만료된 토큰       →", short(http.get(P, headers=token(AI, WORK, ttl=timedelta(seconds=-1)))))
        forged = raw[:-4] + ("AAAA" if not raw.endswith("AAAA") else "BBBB")
        print("서명이 틀린 토큰  →", short(http.get(P, headers={"Authorization": f"Bearer {forged}"})))
        print("읽기 전용 토큰으로 Action →",
              short(http.post(f"{P}/actions/{REQ}/apply", headers=token(AI, ["objects:read"]),
                              json={"target": "R00006", "dry_run": True})))

        # ------------------------------------------------------------------ 2
        section("2. 역할별 조회 — 같은 요청, 다른 결과")
        print(f"{'':8s} {'고객':>6s} {'자료수집중 신고':>14s} {'미완료 업무':>10s}  고객 속성 중 감춘 것")
        for name, h in (("세무사", tax), ("실무자", staff), ("AI비서", ai)):
            cust = http.post(f"{P}/objects/Customer/search", headers=h, json={"limit": 1}).json()
            rets = http.post(f"{P}/objects/TaxReturn/aggregate", headers=h,
                             json={"where": [{"field": "status", "op": "eq", "value": "자료수집중"}]}).json()
            tasks = http.post(f"{P}/objects/Task/aggregate", headers=h,
                              json={"where": [{"field": "status", "op": "in", "value": ["대기", "진행중"]}]}).json()
            print(f"{name:8s} {cust['total']:>6d} {rets['count']:>14d} {tasks['count']:>10d}  "
                  f"{', '.join(cust['hidden']) or '-'}")
        me = http.get(P, headers=ai).json()["you"]
        print("\nAI비서가 메타데이터에서 읽는 '나에게 적용되는 정책':")
        print("  범위:", ", ".join(me["scopes"]))
        for n, d in {**me["row_policies"], **me["property_policies"]}.items():
            print(f"  {n}: {d}")
        c8 = http.get(f"{P}/objects/Customer/C0008", headers=ai).json()
        print(f"\nGET Customer/C0008 (AI비서) → 속성 {len(c8['properties'])}개, hidden {c8['hidden']}")
        other = engine.objects("Customer").where({"field": "accountant_id", "op": "ne", "value": "A003"}) \
            .order_by("customer_id").first().pk
        own = engine.objects("Customer").where(accountant_id="A003").order_by("customer_id").first().pk
        print(f"GET Customer/{own} (실무자 A003, 본인 담당) →", http.get(f"{P}/objects/Customer/{own}",
                                                                   headers=staff).status_code)
        print(f"GET Customer/{other} (실무자 A003, 다른 담당) →", short(http.get(f"{P}/objects/Customer/{other}",
                                                                          headers=staff)))

        # ------------------------------------------------------------------ 3
        section("3. 정책 위반 — 감춘 속성, 범위 밖 객체")
        print("AI비서: 전화번호로 고객 검색 →",
              short(http.post(f"{P}/objects/Customer/search", headers=ai,
                              json={"where": [{"field": "phone", "op": "eq", "value": "010-0000-0008"}]})))
        not_own = engine.actions.candidates(REQ).where(
            {"field": "customer.accountant_id", "op": "ne", "value": "A003"}).order_by("return_id").first().pk
        print(f"실무자 A003: 다른 담당 신고 {not_own} 자료 요청 →",
              short(http.post(f"{P}/actions/{REQ}/apply", headers=staff, json={"target": not_own})))
        print(f"실무자 A003: 다른 담당 고객 {other} 위험도 →",
              short(http.post(f"{P}/functions/filing_delay_risk", headers=staff,
                              json={"args": {"customer": other}})))
        rec = http.post(f"{P}/functions/recommend_assignee", headers=ai, json={"args": {"customer": "C0001"}}).json()
        print(f"AI비서: 담당자 추천 → {rec['result']['pk']}, 속성 {sorted(rec['result']['properties'])}, "
              f"hidden {rec['result']['hidden']}")

        # ------------------------------------------------------------------ 4
        section("4. 승인 워크플로 — 미리보기 · 기한 · 알림")
        risky = [r.pk for r in engine.actions.candidates(REQ, actor=AI).where(
            {"field": "customer.risk_level", "op": "eq", "value": "높음"}).order_by("return_id").limit(3)]
        print("AI비서가 실행할 수 있는 고위험 고객의 신고:", risky)
        ids = {}
        for ret in risky:
            r = http.post(f"{P}/actions/{REQ}/apply", headers=ai, json={"target": ret})
            ids[ret] = r.json()["approval_id"]
            print(f"  {ret} → {short(r)}")
        print("\nGET .../approvals (세무사) — 승인자가 보는 목록")
        for a in http.get(f"{P}/approvals", headers=tax).json()["data"]:
            print(f"  {a['target_pk']}  기한 {a['expires_at'][:16].replace('T', ' ')}  변경 미리보기:")
            for c in a["preview"]:
                after = ", ".join(f"{k}={v}" for k, v in c["after"].items()
                                  if k in ("status", "task_type", "assignee_id"))
                print(f"      {c['op']} {c['object_type']}({c['pk']}) {after}")
        print("  GET .../approvals (AI비서) →", short(http.get(f"{P}/approvals", headers=ai)))

        b, a, c = risky                          # b: 자료 두 건을 요청하는 신고 (미리보기 대조용)
        print(f"\n[정상] 세무사가 {a} 승인 →", short(http.post(f"{P}/approvals/{ids[a]}/decide", headers=tax,
                                                           json={"comment": "확인"})))

        doc = engine.get("TaxReturn", b).submissions.where(status="미요청").order_by("submission_id").first().pk
        print(f"\n[달라짐] 승인 전에 세무사가 {b} 의 자료 {doc} 를 직접 접수 →",
              short(http.post(f"{P}/actions/record_document_submission/apply", headers=tax, json={"target": doc})))
        print(f"         세무사가 {b} 승인 →", short(http.post(f"{P}/approvals/{ids[b]}/decide", headers=tax, json={})))

        engine.conn.execute("UPDATE minionto.approval_requests SET expires_at = expires_at - interval '25 hours' "
                            "WHERE approval_id = %s", (ids[c],))
        print(f"\n[기한] {c} 요청의 기한이 지났다고 하자 (기한을 25시간 앞당김)")
        print(f"  세무사가 {c} 승인 →", short(http.post(f"{P}/approvals/{ids[c]}/decide", headers=tax, json={})))

        status = engine.conn.execute(
            "SELECT target_pk, status, comment FROM minionto.approval_requests WHERE approval_id = ANY(%s::uuid[]) "
            "ORDER BY target_pk", (list(ids.values()),)).fetchall()
        print("\n승인 요청의 최종 상태:", ", ".join(f"{r['target_pk']} {r['status']}" for r in status))

        # ------------------------------------------------------------------ 5
        section("5. 기록 — 정책 판단과 알림 이벤트")
        for r in engine.conn.execute(
                """SELECT decision, kind, actor->>'id' AS who, object_type, target_pk, policy, detail
                   FROM minionto.policy_decisions WHERE decision_id > %s ORDER BY decision_id""",
                (start,)).fetchall():
            extra = r["detail"].get("scope") or r["detail"].get("property") or r["detail"].get("use") or \
                f"{r['detail'].get('rows')}행, 감춤 {len(r['detail'].get('hidden', []))}"
            print(f"  {r['decision']:5s} {r['kind']:8s} {r['who']:9s} {r['object_type'] or '':10s} "
                  f"{r['target_pk'] or '':7s} {r['policy'] or '':15s} {extra}")
        print()
        for r in engine.conn.execute(
                """SELECT event_type, target_pk, payload->>'decision' AS decision
                   FROM minionto.outbox WHERE event_type LIKE 'approval%%' ORDER BY event_id""").fetchall():
            print(f"  outbox {r['event_type']:18s} {r['target_pk']}  {r['decision'] or ''}")
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
