"""35장. 세 번째 도메인 — 설비 정비를 정의만으로 올리고 업무를 돌린다

    uv run python 10-benchmarks/reuse/maintenance_scenario.py
    uv run python 10-benchmarks/reuse/maintenance_scenario.py --llm      # 7단계: LLM 에이전트 3회 (처음 데이터에서)

1. 정의와 데이터     — 객체 3 · 링크 3 · Function 3 · Action 6 (엔진 코드 변경 없음)
2. 점검 지시         — 점검 주기가 지난 설비를 찾아 AI 에이전트가 예방점검 지시 (중복 방지)
3. 고장 신고·배정    — 정비원이 고장 신고 → 팀장이 배정 (라인·동시 작업 한도)
4. 승인과 작업       — 300만 원 이상 수리는 팀장 승인 뒤 시작, 완료하면 설비가 가동으로 (되쓰기)
5. 막히는 요청       — 상태 전이·권한·라인·한도·AI 의 고장 신고
6. 권한 정책과 감사   — 정비원에게 보이는 범위, AI 에게 감춘 속성, 감사 기록

모든 DB 변경은 롤백한다.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "03-ontology-engine"))

from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402
from minionto.audit import AuditLog  # noqa: E402

MT = ROOT / "03-ontology-engine" / "ontologies" / "maintenance" / "ontology.yaml"
LEAD_A = {"id": "E01", "role": "정비팀장", "type": "human", "line": "A라인"}
TECH_A = {"id": "E02", "role": "정비원", "type": "human", "line": "A라인"}
TECH_B = {"id": "E06", "role": "정비원", "type": "human", "line": "B라인"}
AI = {"id": "agent-mt", "role": "AI비서", "type": "agent"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show(label: str, r) -> None:
    print(f"  {label:34s} → {r.status}" + (f": {' / '.join(r.messages)}" if r.messages else ""))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with OntologyClient.open(MT, today="2027-01-18") as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        o = c.onto
        section("1. 정의와 데이터 — 같은 엔진, 세 번째 도메인")
        print(f"{o.ontology.display_name} v{o.ontology.version}: 객체 {len(o.object_types)} · 링크 {len(o.link_types)} · "
              f"Function {len(o.functions)} · Action {len(o.actions)} · 역할 {', '.join(o.roles)}")
        print("  " + " · ".join(f"{t} {c.objects(t).count()}" for t in o.object_types))
        print(f"  작업 지시 상태: {dict(Counter(w.status for w in c.objects('WorkOrder').all()))}")

        section("2. 점검 지시 — AI 에이전트")
        due = [e for e in c.objects("Equipment").where(status="가동").order_by("equipment_id").all()
               if c.functions.call("inspection_due", equipment=e.pk)]
        print(f"  점검 주기가 지난 가동 설비 {len(due)}대: {[e.pk for e in due]}")
        st = Counter()
        for e in due:
            r = c.actions.apply("open_inspection", e.pk, actor=AI, params={"note": "점검 주기 경과"})
            st[r.status] += 1
            if r.status != "completed":
                st_msg = r.messages[0] if r.messages else ""
                print(f"    {e.pk} {r.status}: {st_msg}")
        print(f"  예방점검 지시 {dict(st)}")
        show("같은 설비에 한 번 더", c.actions.apply("open_inspection", due[0].pk, actor=AI))

        section("3. 고장 신고와 배정")
        repair_open = {w.equipment_id for w in c.objects("WorkOrder").where(
            kind="고장수리", status=["접수", "배정", "작업중"]).all()}
        eq = next(e for e in c.objects("Equipment").where(line="A라인", status="가동").order_by("equipment_id").all()
                  if e.pk not in repair_open and e.technician_id != TECH_A["id"])     # 남이 담당하는 같은 라인 설비
        print(f"  {eq.pk} {eq.equipment_name} ({eq.line}, 중요도 {eq.criticality}, {eq.status}, 담당 {eq.technician_id})")
        r = c.actions.apply("report_failure", eq.pk, actor=TECH_A,
                            params={"estimated_cost": 4500000, "note": "유압 누유"})
        show("정비원 E02 고장 신고 (450만 원)", r)
        if r.status != "completed":                     # 정비원이 신고하지 못하면 팀장이 대신 신고한다
            r = c.actions.apply("report_failure", eq.pk, actor=LEAD_A,
                                params={"estimated_cost": 4500000, "note": "유압 누유"})
            show("팀장 E01 이 대신 고장 신고", r)
        wo = c.objects("WorkOrder").where(equipment_id=eq.pk, kind="고장수리", status="접수").first()
        print(f"  생성된 작업 지시 {wo.pk}, 설비 상태 → {c.get('Equipment', eq.pk).status}")
        load = {t.pk: c.objects("WorkOrder").where(technician_id=t.pk, status=["배정", "작업중"]).count()
                for t in c.objects("Technician").where(line="A라인").all()}
        print(f"  A라인 정비원의 진행 중 작업 수: {load} (한도 4)")
        show("팀장이 B라인 E06 배정", c.actions.apply("assign_technician", wo.pk, actor=LEAD_A,
                                                params={"technician": "E06"}))
        full = next((k for k, v in load.items() if v >= 4), None)
        if full:
            show(f"팀장이 한도 찬 {full} 배정", c.actions.apply("assign_technician", wo.pk, actor=LEAD_A,
                                                         params={"technician": full}))
        free = min((k for k in load if k != "E01"), key=lambda k: load[k])
        show(f"팀장이 {free} 배정", c.actions.apply("assign_technician", wo.pk, actor=LEAD_A,
                                               params={"technician": free}))
        worker = {"id": free, "role": "정비원", "type": "human", "line": "A라인"}

        section("4. 승인과 작업 — 300만 원 이상 수리")
        r = c.actions.apply("start_work", wo.pk, actor=worker)
        show(f"{free} 작업 시작", r)
        if r.status == "approval_required":
            d = c.actions.decide(r.approval_id, LEAD_A, approve=True, comment="부품 재고 확인")
            print(f"  팀장 승인 → {d.status}, 작업 지시 {c.get('WorkOrder', wo.pk).status}")
        r = c.actions.apply("complete_work", wo.pk, actor=worker)
        show(f"{free} 작업 완료", r)
        e2 = c.get("Equipment", eq.pk)
        print(f"  설비 {eq.pk}: 상태 {e2.status}, 마지막 점검일 {e2.last_inspected} (되쓰기)")
        if r.status == "completed":
            trace = AuditLog(c).trace(r.execution_id)
            print(f"  감사: {trace['execution']['action']} 실행자 {trace['execution']['actor']['id']}, "
                  f"변경 {len(trace['changes'])}건, 이벤트 {[e['event_type'] for e in trace['events']]}")

        section("5. 막히는 요청")
        done = c.objects("WorkOrder").where(status="완료").order_by("order_id").first()
        doer = {"id": done.technician_id, "role": "정비원", "type": "human",
                "line": c.get("Technician", done.technician_id).line}
        open_ = c.objects("WorkOrder").where(status="접수").order_by("order_id").first().pk
        assigned = [w for w in c.objects("WorkOrder").where(status="배정").order_by("order_id").all()
                    if w.technician_id != TECH_B["id"]][0]
        cases = [
            (f"완료된 작업을 다시 시작 ({doer['id']})", lambda: c.actions.apply("start_work", done.pk, actor=doer)),
            ("정비원이 배정", lambda: c.actions.apply("assign_technician", open_, actor=TECH_A,
                                                 params={"technician": "E03"})),
            (f"남({assigned.technician_id})의 작업을 E06 이 시작", lambda: c.actions.apply("start_work", assigned.pk, actor=TECH_B)),
            ("AI 가 고장 신고", lambda: c.actions.apply("report_failure", "M005", actor=AI,
                                                   params={"estimated_cost": 100000, "note": "소음"})),
            ("폐기 설비 고장 신고", lambda: c.actions.apply("report_failure", "M040", actor=LEAD_A,
                                                    params={"estimated_cost": 100000, "note": "x"})),
        ]
        for name, fn in cases:
            show(name, fn())

        section("6. 권한 정책과 감사")
        for a in (LEAD_A, TECH_A, TECH_B, AI):
            n_eq = c.objects("Equipment").secure(a).count()
            n_wo = c.objects("WorkOrder").secure(a).count()
            print(f"  {a['id']:8s} {a['role']:5s} 보이는 설비 {n_eq:2d} · 작업 지시 {n_wo:2d}")
        s = AgentSession(c, AI)
        print(f"  AI 컨텍스트: 정비원의 감춘 속성 {s.context()['object_types']['Technician'].get('hidden')}, "
              f"실행할 수 있는 Action {list(s.context()['actions'])}")
        m = AuditLog(c).metrics()
        acts = Counter()
        for a in m["actions"]:
            acts[f"{a['actor_type']}·{a['status']}"] += a["n"]
        print(f"  감사 기록(실행자 유형·결과): {dict(sorted(acts.items()))}")
        print(f"  승인 {[(a['status'], a['n']) for a in m['approvals']]} · 아웃박스 {[(a['status'], a['n']) for a in m['outbox']]}")
        print(f"  감사 로그 점검: 문제 {len(AuditLog(c).verify())}건")
    if "--llm" in sys.argv:
        llm()
    print("\n(모든 변경을 롤백했습니다)")
    return 0


def llm(runs: int = 3, max_steps: int = 30) -> None:
    """7. LLM 에이전트 — 처음 데이터 상태에서, 회마다 롤백한다"""
    from dotenv import load_dotenv

    from minionto.llm import ChatClient, run_agent
    load_dotenv(ROOT / ".env")
    chat = ChatClient()
    section(f"7. LLM 에이전트 ({runs}회, 최대 {max_steps}단계, 각 회 롤백)")
    with OntologyClient.open(MT, today="2027-01-18") as c:
        c.actions.ensure_tables()
        expected = sorted(e.pk for e in c.objects("Equipment").where(line="A라인", status="가동").all()
                          if c.actions.check("open_inspection", e.pk, actor=AI).status == "ready")
        print(f"  정답(지시할 수 있는 A라인 설비): {expected}")
        for k in range(runs):
            with c.conn.transaction(force_rollback=True):
                s = AgentSession(c, AI, task_id=f"mt-llm{k}")
                r = run_agent(chat, s, "점검 주기가 지난 A라인 설비를 찾아서 예방점검을 지시해 줘.", max_steps=max_steps)
                done = Counter(x["result"].get("status") for x in r["trace"] if x["tool"] == "run_action")
                made = sorted(w.equipment_id for w in c.objects("WorkOrder").where(
                    kind="예방점검", status="접수", created_by="agent", opened_at="2027-01-18").all())
                print(f"  LLM #{k + 1}: 단계 {r['steps']}, 도구 {dict(Counter(x['tool'] for x in r['trace']))}, "
                      f"입력 토큰 {r['usage']['prompt_tokens']:,}")
                print(f"     run_action {dict(done)} · 만든 지시 {made} · 정답과 같음 {made == expected}")
                print(f"     답: {(r['answer'] or r['stopped'])[:200]}".replace(chr(10), " "))


if __name__ == "__main__":
    raise SystemExit(main())
