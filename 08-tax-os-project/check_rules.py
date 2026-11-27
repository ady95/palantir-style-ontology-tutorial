"""33장. 업무 규칙 목록 ↔ 정의 ↔ 동작 대조

    uv run python 08-tax-os-project/check_rules.py

1. 정의에서 규칙 요소를 모두 뽑는다 (실행 조건·권한·승인·상태 머신·정책·DB 제약)
2. 목록의 규칙마다 구현 위치가 정의에 실제로 있는지 본다
3. 규칙마다 시나리오를 롤백 안에서 실행해 기대대로인지 본다
4. 정의에는 있는데 목록에 없는 규칙(문서화되지 않은 규칙)을 찾는다
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))

from minionto import OntologyClient  # noqa: E402

TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"
RULES = HERE / "business_rules.yaml"
AS_OF = "2027-01-18"
ACTORS = {"AI": {"id": "agent-01", "role": "AI비서", "type": "agent"},
          "TAX": {"id": "A001", "role": "세무사", "type": "human"},
          "STAFF": {"id": "A003", "role": "실무자", "type": "human"},
          "SYSTEM": {"id": "reasoner", "role": "시스템", "type": "system"}}


# ---------------------------------------------------------------------- 1. 정의 속 규칙 요소
def inventory(c: OntologyClient) -> dict[str, dict]:
    o = c.onto
    inv: dict[str, dict] = {}
    for an, a in o.actions.items():
        for r in a.preconditions:
            inv[f"precondition:{an}:{r.message}"] = {"kind": "실행조건", "action": an, "text": r.message}
        inv[f"permission:{an}"] = {"kind": "권한", "action": an,
                                   "text": f"{'/'.join(a.permission.roles)} · {'/'.join(a.permission.actor_types)}"}
        if a.approval:
            inv[f"approval:{an}"] = {"kind": "승인", "action": an, "text": a.approval.reason or ""}
    for on, ot in o.object_types.items():
        if ot.state_machine:
            inv[f"state_machine:{on}"] = {"kind": "상태전이", "text": f"{on}.{ot.state_machine.property}"}
    for pn in o.security.row_policies:
        inv[f"policy:{pn}"] = {"kind": "정책", "text": o.security.row_policies[pn].description or pn}
    for pn in o.security.property_policies:
        inv[f"policy:{pn}"] = {"kind": "정책", "text": o.security.property_policies[pn].description or pn}
    for r in c.conn.execute("SELECT conname, pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE contype = 'c' "
                            "AND connamespace = 'public'::regnamespace ORDER BY 1").fetchall():
        enum_only = " = ANY " in r["d"] and " AND " not in r["d"] and " OR " not in r["d"]
        inv[f"db:{r['conname']}"] = {"kind": "값목록" if enum_only else "DB제약", "text": r["d"][:80]}
    return inv


def locate(impl: dict, inv: dict) -> list[str]:
    """목록의 implemented_by → 정의 속 요소 키들"""
    if "precondition" in impl:
        return [k for k, v in inv.items() if k.startswith(f"precondition:{impl['action']}:")
                and impl["precondition"] in v["text"]]
    for kind in ("permission", "approval", "state_machine", "policy", "db"):
        if kind in impl:
            key = f"{kind}:{impl[kind]}"
            return [key] if key in inv else []
    if impl.get("engine") == "idempotency_key":
        return ["engine:idempotency_key"]                     # 엔진 기능 (22장) — 정의 밖
    return []


# ---------------------------------------------------------------------- 3. 시나리오
def run(c: OntologyClient, sc: dict) -> tuple[bool, str]:
    actor = ACTORS.get(sc.get("actor", ""))
    exp = sc["expect"]
    with c.conn.transaction(force_rollback=True):
        for sql in sc.get("setup", []):
            c.conn.execute(sql)
        if sc.get("today"):
            c.today = __import__("datetime").date.fromisoformat(sc["today"])
        try:
            target = sc.get("target")
            if sc.get("target_sql"):
                target = c.conn.execute(sc["target_sql"]).fetchone()
                target = next(iter(target.values())) if target else None
            do = sc["do"]
            if do == "sql":
                try:
                    with c.conn.transaction():
                        c.conn.execute(sc["sql"])
                    return False, "막히지 않음"
                except Exception as e:  # noqa: BLE001
                    name = type(e).__name__
                    return name == exp["error"], name
            if do == "count":
                n = c.objects(sc["object_type"]).secure(actor).count()
                return n == exp["count"], f"{n}건"
            if do == "hidden":
                obj = c.get(sc["object_type"], target, actor=actor)
                h = sorted(obj.restricted)
                return h == sorted(exp["hidden"]), f"감춤 {h}"
            if do == "decide":
                r = c.actions.apply(sc["action"], target, actor=ACTORS[sc["request_by"]])
                res = c.actions.decide(r.approval_id, actor)
            elif do == "apply_twice":
                c.actions.apply(sc["action"], target, actor=actor, idempotency_key="rule-check")
                res = c.actions.apply(sc["action"], target, actor=actor, idempotency_key="rule-check")
                status = "replayed" if res.replayed else res.status
                return status == exp["status"], status
            else:
                res = c.actions.apply(sc["action"], target, actor=actor, params=sc.get("params") or {})
            ok = res.status == exp["status"] and (
                "message" not in exp or any(exp["message"] in m for m in res.messages))
            return ok, f"{res.status}: {' / '.join(res.messages)[:80]}"
        finally:
            c.today = __import__("datetime").date.fromisoformat(AS_OF)


def check() -> dict:
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))["rules"]
    out = {"rules": [], "undocumented": []}
    with OntologyClient.open(TAXOS, today=AS_OF) as c, c.conn.transaction(force_rollback=True):
        c.actions.ensure_tables()
        inv = inventory(c)
        used: set[str] = set()
        for r in rules:
            keys = locate(r["implemented_by"], inv)
            used |= set(keys)
            ok, note = run(c, r["scenario"])
            out["rules"].append({"id": r["id"], "rule": r["rule"], "kind": r["kind"], "found": bool(keys),
                                 "passed": ok, "note": note})
        out["undocumented"] = [{"key": k, **v} for k, v in inv.items() if k not in used]
        out["inventory"] = len(inv)
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    res = check()
    print(f"{'ID':4s} {'종류':6s} {'구현':4s} {'동작':4s} 규칙 — 결과")
    for r in res["rules"]:
        print(f"{r['id']:4s} {r['kind']:6s} {'있음' if r['found'] else '없음':4s} {'통과' if r['passed'] else '실패':4s} "
              f"{r['rule']} — {r['note']}")
    n = len(res["rules"])
    print(f"\n목록 {n}개: 구현 확인 {sum(r['found'] for r in res['rules'])}/{n}, "
          f"동작 확인 {sum(r['passed'] for r in res['rules'])}/{n}")
    print(f"정의 속 규칙 요소 {res['inventory']}개 중 목록에 없는 것 {len(res['undocumented'])}개:")
    by: dict[str, list[str]] = {}
    for u in res["undocumented"]:
        by.setdefault(u["kind"], []).append(u["key"].split(":", 1)[1])
    for k, v in by.items():
        print(f"  {k} {len(v)}: {', '.join(v[:6])}{' …' if len(v) > 6 else ''}")
    return 0 if all(r["found"] and r["passed"] for r in res["rules"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
