"""30장. 논리적 추론과 업무 규칙 결합 — 추론 기반 위험 고객 분류

    uv run python 06-hybrid-ontology/30_reasoning.py

1. 추론          — 운영 DB → RDF → OWL-RL, 정의 클래스 소속과 근거. 같은 조건의 운영 질의와 비교
2. 모순          — 공리와 맞지 않는 사실이 있으면 아무것도 반영하지 않는다
3. 동기화        — 추론 결과를 시스템 Action(assign_segment)으로 반영, 다시 돌리면 할 일 없음
4. 데이터 변경    — 위험 이벤트가 늘고 줄면 분류가 붙고 풀린다
5. 사람의 판단    — 세무사가 해제한 분류는 근거가 그대로면 다시 붙이지 않는다
6. 열린 세계      — 일부만 보는 그래프로는 해제하지 않는다
7. Action 후보   — 추론 결과(반복 위험)를 실행 조건으로 쓰는 우선 연락 업무

모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))

from rdflib import RDF, Graph, URIRef  # noqa: E402

from minionto import OntologyClient  # noqa: E402
from minionto.reasoning import SYSTEM, infer, plan_segments, sync_segments  # noqa: E402
from minionto.semantic import Mapping  # noqa: E402

MAPPING = HERE / "taxos_mapping.yaml"
AS_OF = "2027-01-18"
TAX = {"id": "A001", "role": "세무사", "type": "human"}
STAFF = {"id": "A003", "role": "실무자", "type": "human"}
TX = "https://example.org/taxos#"
TYPES = {"tx:LateFiler": ["기한후신고"], "tx:UnreachableCustomer": ["연락두절"],
         "tx:RepeatRiskCustomer": ["기한후신고", "가산세부과"]}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def ops(plan: list[dict]) -> str:
    return ", ".join(f"{k} {v}" for k, v in Counter(p["op"] for p in plan).items()) or "할 일 없음"


def rule_based(c: OntologyClient, types: list[str]) -> list[str]:
    """같은 분류를 운영형 조건으로 — 위험 이벤트 유형마다 some"""
    q = c.objects("Customer")
    for t in types:
        q = q.where({"some": {"link": "risk_events", "where": {"field": "event_type", "op": "eq", "value": t}}})
    return sorted(o.pk for o in q.all())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    m = Mapping.load(MAPPING)
    with OntologyClient.open(ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml",
                             today=AS_OF) as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()

        section("1. 추론 — 정의 클래스 소속과 근거")
        inf = infer(c, m)
        print(f"{inf.source} · 트리플 {inf.triples[0]:,} → {inf.triples[1]:,} · {inf.seconds}초 · 모순 {len(inf.errors)}건")
        for cls, members in inf.members.items():
            name = m.short(cls)
            t0 = time.perf_counter()
            rule = rule_based(c, TYPES[name])
            ms = (time.perf_counter() - t0) * 1000
            first = next(iter(members.items()))
            print(f"  {name:24s} {len(members):3d}명 · 운영 질의 {len(rule):3d}명 ({ms:.0f}ms) · "
                  f"{'같음' if rule == sorted(members) else '다름'} · 예: {first[0]} 근거 {first[1]}")

        section("2. 모순이 있으면 반영하지 않는다")
        corp = c.objects("Customer").where(business_type="법인").order_by("customer_id").first().pk
        bad = Graph()
        bad.add((URIRef(f"https://example.org/taxos/data/customer/{corp}"), RDF.type, URIRef(TX + "SimplifiedTaxpayer")))
        wrong = infer(c, m, extra=bad)
        print(f"  법인 {corp} 를 간이과세자라고 덧붙이면 → 모순 {len(wrong.errors)}건")
        try:
            plan_segments(c, m, wrong)
        except ValueError as e:
            print(f"  반영 거부: {str(e)[:100]}…")

        section("3. 동기화 — 시스템 Action 으로만")
        plan = plan_segments(c, m, inf)
        print(f"  계획: {ops(plan)}")
        res = sync_segments(c, m, inf)
        print(f"  반영: {', '.join(f'{k} {v}' for k, v in Counter(r['status'] for r in res).items())}")
        segs = c.objects("CustomerSegment").where(status="유효").all()
        print(f"  CustomerSegment 유효 {len(segs)}건: {dict(Counter(s.segment for s in segs))}")
        logged = c.conn.execute("SELECT count(*) AS n FROM minionto.action_log WHERE action = 'assign_segment' "
                                "AND actor->>'id' = %s AND status = 'completed'", (SYSTEM["id"],)).fetchone()["n"]
        print(f"  실행 기록(action_log): assign_segment {logged}건, 실행자 {SYSTEM['id']}({SYSTEM['type']})")
        print(f"  다시 계획: {ops(plan_segments(c, m, infer(c, m)))}")

        section("4. 데이터가 바뀌면")
        only_late = sorted(set(rule_based(c, ["기한후신고"])) - set(rule_based(c, ["가산세부과"])))[0]
        c.conn.execute("INSERT INTO risk_events (event_id, customer_id, event_type, occurred_at, note) "
                       "VALUES ('E9001', %s, '가산세부과', '2027-01-10', '실습: 새 위험 이벤트')", (only_late,))
        unreach = rule_based(c, ["연락두절"])[0]
        c.conn.execute("DELETE FROM risk_events WHERE customer_id = %s AND event_type = '연락두절'", (unreach,))
        print(f"  {only_late} 에 가산세부과 이벤트 추가, {unreach} 의 연락두절 이벤트 삭제 (외부 적재라고 치자)")
        plan = plan_segments(c, m, infer(c, m))
        for p in plan:
            print(f"  계획 {p['op']:7s} {p['customer']} {p['segment']} {p.get('evidence', '')}")
        res = sync_segments(c, m, infer(c, m))
        print(f"  반영: {', '.join(f'{k} {v}' for k, v in Counter(r['status'] for r in res).items())}")

        section("5. 사람의 판단이 우선")
        seg = c.objects("CustomerSegment").where(segment="반복위험", status="유효").order_by("customer_id").first()
        r = c.actions.apply("release_segment", seg.pk, actor=TAX, params={"reason": "확인 결과 해당 없음"})
        print(f"  세무사 A001 이 {seg.customer_id} 의 반복위험 해제: {r.status}")
        for p in plan_segments(c, m, infer(c, m)):
            print(f"  다음 동기화 계획: {p['op']} {p['customer']} {p['segment']} — {p.get('why', '')}")

        section("6. 열린 세계 — 일부만 보는 그래프로는 해제하지 않는다")
        part = infer(c, m, actor=STAFF)
        plan = plan_segments(c, m, part)
        print(f"  실무자 A003 권한으로 추론: 완전한 그래프 {part.complete} · "
              f"{', '.join(f'{m.short(k)} {v}' for k, v in part.count().items())}")
        print(f"  계획: {ops(plan)} — 보이지 않는 고객의 분류는 해제하지 않고 그대로 둔다")

        section("7. 추론 결과로 Action 후보 만들기")
        cand = sorted(o.pk for o in c.actions.candidates("create_priority_contact", actor=TAX).all())
        both = set(rule_based(c, ["기한후신고", "가산세부과"]))
        collecting = {o.pk for o in c.objects("Customer").where(
            {"some": {"link": "tax_returns", "where": {"field": "status", "op": "eq", "value": "자료수집중"}}}).all()}
        print(f"  우선 연락 업무 후보 {len(cand)}명 (반복위험 유효 + 자료수집중 신고 + 진행 중 고객연락 없음)")
        open_contact = {o.pk for o in c.objects("Customer").where({"some": {"link": "tasks", "where": {"all": [
            {"field": "task_type", "op": "eq", "value": "고객연락"},
            {"field": "status", "op": "in", "value": ["대기", "진행중"]}]}}}).all()}
        ref = sorted((both & collecting) - open_contact - {seg.customer_id})
        print(f"  운영 조건으로 다시 계산(위험 이벤트 두 종류 + 자료수집중 − 진행 중 고객연락 − 사람이 해제한 고객): "
              f"{len(ref)}명 · {'같음' if ref == cand else '다름'}")
        r = c.actions.apply("create_priority_contact", cand[0], actor=TAX, params={"note": "반복 위험 — 직접 연락"})
        print(f"  {cand[0]} 우선 연락 업무: {r.status} {r.changes[0]['pk'] if r.changes else ''}")
        r = c.actions.apply("create_priority_contact", seg.customer_id, actor=TAX)
        print(f"  {seg.customer_id} (세무사가 분류 해제): {r.status} — {r.messages[0]}")
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
