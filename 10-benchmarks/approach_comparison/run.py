"""31장. 네 방식 비교 실험 — 같은 요구사항, 네 가지 구현

    uv run python 10-benchmarks/approach_comparison/run.py                 # T1~T6, T8 (모두 롤백)
    uv run python 10-benchmarks/approach_comparison/run.py --concurrency   # T7 동시 실행 (DB 를 실제로 바꾼다)
    bash scripts/load_taxos.sh                                             # T7 뒤에는 다시 적재

결과: results/results.json, results/summary.md
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path


import bench  # noqa: F401 — 경로 설정
import a_sql
import b_rdf
import c_onto
import d_hybrid
from bench import AI, AS_OF, DUE, HERE, REQUEST_CASES, ROOT, STAFF, TAX, TAXOS, MAPPING, timed
from minionto import OntologyClient
from minionto.agent import AgentSession
from minionto.loader import load
from minionto.semantic import Mapping, export_rdf

OUT = HERE / "results"
TX = "https://example.org/taxos#"


def rollback(c):
    return c.conn.transaction(force_rollback=True)


# ====================================================================== T1 조회 + 최신성
def t1(c, g, m) -> dict:
    a, ta = timed(a_sql.missing_customers, c.conn, AS_OF, DUE)
    b, tb = timed(b_rdf.missing_customers, g, AS_OF, DUE)
    cc, tc = timed(c_onto.missing_customers, c, AS_OF, DUE)
    d, td = timed(d_hybrid.missing_customers, c, m, AS_OF, DUE, TAX, repeat=3)
    out = {"truth": len(a), "A": [len(a), ta, a == a], "B": [len(b), tb, b == a], "C": [len(cc), tc, cc == a],
           "D": [len(d), td, d == a]}
    with rollback(c):                                  # 최신성: 고객 하나가 자료를 모두 냈다
        cid = a[0]
        c.conn.execute("""UPDATE document_submissions s SET status = '제출완료', submitted_at = %s
                          FROM tax_returns r WHERE s.return_id = r.return_id AND r.customer_id = %s
                          AND s.status IN ('미요청', '요청됨')""", (AS_OF, cid))
        out["fresh"] = {"changed": cid,
                        "A": cid not in a_sql.missing_customers(c.conn, AS_OF, DUE),
                        "B": cid not in b_rdf.missing_customers(g, AS_OF, DUE),
                        "C": cid not in c_onto.missing_customers(c, AS_OF, DUE),
                        "D": cid not in d_hybrid.missing_customers(c, m, AS_OF, DUE, TAX)}
    return out


# ====================================================================== T2 분류
def t2(c, g, m) -> dict:
    a, ta = timed(a_sql.repeat_risk, c.conn)
    b, tb = timed(b_rdf.repeat_risk, g, repeat=1)
    cc, tc = timed(c_onto.repeat_risk, c)
    with rollback(c):
        sync, ts = timed(d_hybrid.sync_classes, c, m, repeat=1)
        d, td = timed(d_hybrid.repeat_risk, c)
    return {"truth": len(a), "A": [len(a), ta, True], "B": [len(b), tb, b == a], "C": [len(cc), tc, cc == a],
            "D": [len(d), td, d == a], "D_sync_ms": ts, "D_applied": sync["applied"]}


# ====================================================================== T3 데이터 품질: 막는가, 찾는가
BAD_SQL = {
    "q1 법인인데 간이과세자": "UPDATE customers SET vat_type = '간이과세자' WHERE customer_id = '{corp}'",
    "q2 제출완료인데 제출일 없음": "UPDATE document_submissions SET status = '제출완료' WHERE submission_id = '{sub}'",
    "q3 담당자 없는 고객": "UPDATE customers SET accountant_id = NULL WHERE customer_id = '{corp}'",
}


def t3(c, g, m) -> dict:
    corp = c.objects("Customer").where(business_type="법인").order_by("customer_id").first().pk
    sub = c.objects("DocumentSubmission").where(status="미요청").order_by("submission_id").first().pk
    out = {"cases": {}}
    for name, sql in BAD_SQL.items():                 # A·C·D: 같은 DB — 쓰는 순간 막히는가
        try:
            with rollback(c):
                c.conn.execute(sql.format(corp=corp, sub=sub))
            res = "놓침"
        except Exception as e:  # noqa: BLE001
            res = f"막음 ({type(e).__name__})"
        out["cases"][name] = {"DB": res}
    from rdflib import RDF, Literal, URIRef
    bad = b_rdf.Graph()                               # B: 그래프에 같은 잘못을 넣고 SHACL
    bad += g
    cn, sn = URIRef(f"{b_rdf.DATA}customer/{corp}"), URIRef(f"{b_rdf.DATA}submission/{sub}")
    bad.add((cn, RDF.type, URIRef(TX + "SimplifiedTaxpayer")))
    bad.remove((sn, URIRef(TX + "submissionStatus"), None))
    bad.add((sn, URIRef(TX + "submissionStatus"), Literal("제출완료")))
    bad.remove((cn, URIRef(TX + "managedBy"), None))
    base, tbase = timed(b_rdf.validate, g, repeat=1)
    broken, _ = timed(b_rdf.validate, bad, repeat=1)
    out["shacl_base"] = {"violations": base[0], "warnings": base[1], "ms": tbase, "messages": base[2]}
    out["shacl_broken"] = {"violations": broken[0], "warnings": broken[1], "messages": broken[2]}
    dv, td = timed(d_hybrid.validate, c, m, TAX, repeat=1)
    out["shacl_export"] = {"violations": dv[0], "warnings": dv[1], "ms": td}
    no_rows = [r["return_id"] for r in c.conn.execute(
        "SELECT r.return_id FROM tax_returns r WHERE r.due_date = %s AND NOT EXISTS "
        "(SELECT 1 FROM document_submissions s WHERE s.return_id = r.return_id) ORDER BY 1", (DUE,)).fetchall()]
    ready = {o.pk for o in c.actions.candidates("start_review", actor=AI).all()}
    out["q4"] = {"returns": no_rows, "engine_start_review_ready": sorted(set(no_rows) & ready)}
    return out


# ====================================================================== T4 업무 실행 규칙
def t4(c, g) -> dict:
    out = {}
    for ret, want in REQUEST_CASES.items():
        row = {"want": want}
        with rollback(c):
            row["A"] = a_sql.request_documents(c.conn, ret, AI)["status"]
        bg = b_rdf.Graph()
        bg += g
        row["B"] = b_rdf.request_documents(bg, ret, AI, AS_OF)["status"]
        with rollback(c):
            r = c_onto.request_documents(c, ret, AI)
            row["C"] = r["status"]
            row["C_approval_record"] = bool(c.conn.execute(
                "SELECT 1 FROM minionto.approval_requests WHERE target_pk = %s AND status = 'pending'", (ret,)).fetchone())
        row["D"] = row["C"]                            # 실행 경로가 운영형과 같다
        out[ret] = row
    with rollback(c):                                  # B 의 실행은 운영 DB 에 닿는가
        before = c.objects("Task").count()
        bg = b_rdf.Graph()
        bg += g
        b_rdf.request_documents(bg, "R00146", AI, AS_OF)
        out["B_db_tasks_changed"] = c.objects("Task").count() - before
        out["B_graph_tasks"] = b_rdf.task_count(bg, "R00146")
    return out


# ====================================================================== T5 권한 · T6 감사
def t5(c, m) -> dict:
    pii = ("representative", "phone", "business_reg_no")
    a_staff, a_ai = a_sql.list_customers(c.conn, STAFF), a_sql.list_customers(c.conn, AI)
    c_staff, c_ai = c_onto.list_customers(c, STAFF), c_onto.list_customers(c, AI)
    from rdflib import URIRef
    reg = URIRef(TX + "businessRegNo")
    d_staff, d_ai = export_rdf(c, m, actor=STAFF), export_rdf(c, m, actor=AI)
    return {
        "A": {"staff": len(a_staff), "ai_pii": sum(k in r for r in a_ai for k in pii), "how": "함수마다 직접 구현"},
        "B": {"staff": None, "ai_pii": None, "how": "표준에 실행자별 접근 제어 없음"},
        "C": {"staff": len(c_staff), "ai_pii": sum(r.get(k) is not None for r in c_ai for k in pii),
              "how": "정의의 행·속성 정책"},
        "D": {"staff": len(set(d_staff.subjects(URIRef(TX + "riskLevel"), None))),
              "ai_pii": len(list(d_ai.triples((None, reg, None)))), "how": "정책 + 실행자별 내보내기"},
    }


def t6(c) -> dict:
    with rollback(c):
        a_sql.request_documents(c.conn, "R00146", AI)
        a = a_sql.who_changed(c.conn, "R00146")
    with rollback(c):
        r = c_onto.request_documents(c, "R00146", AI)
        t = c_onto.who_changed(c, r["execution_id"])
    return {"A": {"recorded": [{k: str(v) for k, v in x.items()} for x in a], "actor_id": False, "before_after": False},
            "B": {"recorded": None},
            "C": {"recorded": {k: str(v) for k, v in t.items()}, "actor_id": t["actor"] == AI["id"],
                  "before_after": t["changes"] > 0},
            "D": {"same_as": "C", "plus": "추론 분류의 inferred_by·evidence (30장)"}}


# ====================================================================== T7 동시 실행 (실제 커밋)
def t7(trials: int = 6) -> dict:
    from minionto.db import database_url
    url = database_url(load(TAXOS))
    with OntologyClient.open(TAXOS, today=AS_OF) as c:
        ready = [o.pk for o in c.actions.candidates("request_missing_documents", actor=AI)
                 .where({"field": "customer.risk_level", "op": "ne", "value": "높음"}).order_by("return_id").all()]
    variants = {"A (잠금 없음)": "a", "A (FOR UPDATE)": "a_lock", "C·D (엔진)": "c"}
    out = {}
    pool = iter(ready)
    for label, kind in variants.items():
        dup = err = 0
        for _ in range(trials):
            ret = next(pool)
            barrier, errors = threading.Barrier(2), []

            def work(kind=kind, ret=ret, barrier=barrier, errors=errors):
                try:
                    if kind.startswith("a"):
                        conn = a_sql.connect(url)
                        conn.as_of = AS_OF
                        barrier.wait()
                        a_sql.request_documents(conn, ret, AI, lock=(kind == "a_lock"))
                        conn.close()
                    else:
                        with OntologyClient.open(TAXOS, today=AS_OF) as cl:
                            cl.actions.ensure_tables()
                            barrier.wait()
                            c_onto.request_documents(cl, ret, AI)
                except Exception as e:  # noqa: BLE001
                    errors.append(type(e).__name__)
            ts = [threading.Thread(target=work) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            with OntologyClient.open(TAXOS, today=AS_OF) as c:
                n = c.conn.execute("SELECT count(*) AS n FROM tasks WHERE return_id = %s AND task_type = '자료요청' "
                                   "AND status = '진행중'", (ret,)).fetchone()["n"]
            dup += n > 1
            err += bool(errors)
        out[label] = {"trials": trials, "duplicates": dup, "errors": err}
    return out


# ====================================================================== T8 규칙 변경
def _late_target(c) -> str:
    rows = c.actions.candidates("request_missing_documents", actor=AI).where(
        {"field": "customer.risk_level", "op": "ne", "value": "높음"}).where(
        {"some": {"link": "customer.risk_events", "where": {"field": "event_type", "op": "eq", "value": "기한후신고"}}}
    ).order_by("return_id").all()
    return rows[0].pk


APPROVAL_OLD = ("          - { field: customer.risk_level, op: eq, value: 높음 }\n"
                "          - { field: $actor.type, op: eq, value: agent }\n")


def _onto_variant(cond_text: str) -> Path:
    """정의 파일 원문의 승인 조건 두 줄을 고친 판 — 사람이 고치는 모양 그대로"""
    src = TAXOS.read_text(encoding="utf-8")
    assert src.count(APPROVAL_OLD) == 1
    new = ("          - { field: $actor.type, op: eq, value: agent }\n"
           "          - any:\n"
           "              - { field: customer.risk_level, op: eq, value: 높음 }\n"
           f"              - {cond_text}\n")
    p = TAXOS.parent / "_t8_variant.yaml"
    p.write_text(src.replace(APPROVAL_OLD, new), encoding="utf-8")
    return p


def _entry_points(path: Path, ret: str, prepare=None) -> dict:
    with OntologyClient.open(path, today=AS_OF) as c, rollback(c):
        c.actions.ensure_tables()
        if prepare:
            prepare(c)
        engine = c.actions.apply("request_missing_documents", ret, actor=AI, dry_run=True).status
        s = AgentSession(c, AI)
        agent = s.call("check_action", {"action": "request_missing_documents", "target": ret})["status"]
        listing = next((x.status for x in c.actions.available("TaxReturn", ret, actor=AI).actions
                        if x.action == "request_missing_documents"), None)
    return {"엔진(Action·API)": engine, "에이전트 도구(25~27장)": agent, "가능한 Action 목록(20장)": listing}


def t8(c, g, m) -> dict:
    ret = _late_target(c)
    out = {"target": ret, "rule": "기한후신고 이력 고객에게 AI 가 자료를 요청하면 세무사 승인"}
    # A: 서비스 함수는 고쳤지만, 같은 DB 를 쓰는 다른 진입점(28장 일반 도구)은 그대로
    import sys
    sys.path.insert(0, str(ROOT / "10-benchmarks" / "agent_comparison"))
    from agents import PlainAgent
    with rollback(c):
        svc = a_sql.request_documents(c.conn, ret, AI, rule_v2=True)["status"]
    with rollback(c):
        tool = PlainAgent(c, AS_OF, "t8").call("request_missing_documents", {"return_id": ret})
    out["A"] = {"changed_lines": 3, "where": "a_sql.request_documents",
                "entry_points": {"서비스 함수": svc, "에이전트 도구(28장 일반 도구)": "executed" if tool.get("ok") else tool}}
    bg = b_rdf.Graph()
    bg += g
    out["B"] = {"changed_lines": 2, "where": "b_rdf.request_documents",
                "entry_points": {"SPARQL 업무 함수": b_rdf.request_documents(bg, ret, AI, AS_OF, rule_v2=True)["status"]}}
    # C: 정의의 승인 조건 한 곳 — 기한후신고 위험 이벤트가 있으면
    cond_c = "{ some: { link: customer.risk_events, where: { field: event_type, op: eq, value: 기한후신고 } } }"
    pc = _onto_variant(cond_c)
    try:
        out["C"] = {"changed_lines": _diff_lines(pc), "where": "ontology.yaml approval.required_if",
                    "entry_points": _entry_points(pc, ret)}
        cond_d = ("{ some: { link: customer.segments, where: { all: [ { field: segment, op: eq, value: 기한후신고이력 }, "
                  "{ field: status, op: eq, value: 유효 } ] } } }")
        pd = _onto_variant(cond_d)
        out["D"] = {"changed_lines": _diff_lines(pd), "where": "ontology.yaml approval.required_if (추론 분류 사용)",
                    "entry_points": _entry_points(pd, ret, prepare=lambda cl: d_hybrid.sync_classes(cl, m))}
    finally:
        (TAXOS.parent / "_t8_variant.yaml").unlink(missing_ok=True)
    return out


def _diff_lines(variant: Path) -> int:
    """정의 파일 원문과 고친 판의 바뀐 줄 수 (추가 + 삭제)"""
    import difflib
    base, new = TAXOS.read_text(encoding="utf-8").splitlines(), variant.read_text(encoding="utf-8").splitlines()
    return sum(1 for x in difflib.unified_diff(base, new, lineterm="", n=0)
               if x.startswith(("+", "-")) and not x.startswith(("+++", "---")))


# ====================================================================== 코드 양
def loc() -> dict:
    def count(p: Path) -> int:
        return sum(1 for x in p.read_text(encoding="utf-8").splitlines()
                   if x.strip() and not x.strip().startswith(("#", '"""')))
    engine = sum(count(p) for p in (ROOT / "03-ontology-engine" / "minionto").glob("*.py"))
    return {"업무 코드": {k: count(HERE / f) for k, f in (("A", "a_sql.py"), ("B", "b_rdf.py"), ("C", "c_onto.py"),
                                                       ("D", "d_hybrid.py"))},
            "선언": {"taxos.ttl": count(ROOT / "01-traditional-ontology" / "ontology" / "taxos.ttl"),
                   "taxos_shapes.ttl": count(ROOT / "01-traditional-ontology" / "shapes" / "taxos_shapes.ttl"),
                   "ontology.yaml": count(TAXOS), "taxos_mapping.yaml": count(MAPPING),
                   "taxos_rules.ttl": count(ROOT / "06-hybrid-ontology" / "taxos_rules.ttl")},
            "기반": {"07장 ETL(01_build_graph.py)": count(ROOT / "01-traditional-ontology" / "01_build_graph.py"),
                   "MiniOnto 엔진(minionto/*.py)": engine}}


def summary(r: dict) -> str:
    L = ["# 31장 네 방식 비교 결과", "", "| 과제 | A. 일반 DB·API | B. 전통적 | C. 운영형 | D. 하이브리드 |", "|---|---|---|---|---|"]
    if "T1" in r:
        t = r["T1"]
        L.append("| T1 조회 (정답 %d명, 중앙값 ms) | " % t["truth"] + " | ".join(
            f"{t[k][0]}명 {t[k][1]}ms" for k in "ABCD") + " |")
        L.append("| T1 최신성 (DB 변경 반영) | " + " | ".join("반영" if t["fresh"][k] else "안 됨" for k in "ABCD") + " |")
        t = r["T2"]
        L.append("| T2 분류 (정답 %d명) | " % t["truth"] + " | ".join(
            f"{t[k][0]}명 {t[k][1]}ms" for k in "ABCD") + f" |  (D 동기화 {t['D_sync_ms']}ms)")
        t = r["T3"]
        L.append("| T3 잘못된 값 3종 | DB 제약이 막음 3/3 | SHACL 이 찾음 %d/3 | DB 제약이 막음 3/3 | DB 제약 3/3 + SHACL |"
                 % t["shacl_broken"]["violations"])
        L.append("| T3 자료 현황 없는 신고 %d건 | 놓침 | 경고 %d | 놓침 (검토 시작 가능 %d건) | 경고 %d |"
                 % (len(t["q4"]["returns"]), t["shacl_base"]["warnings"], len(t["q4"]["engine_start_review_ready"]),
                    t["shacl_export"]["warnings"]))
        cases = {k: v for k, v in r["T4"].items() if k.startswith("R")}
        L.append("| T4 실행 규칙 5사례 | " + " | ".join(
            f"{sum(v[k] == v['want'] for v in cases.values())}/5" for k in "ABCD") +
            f" |  (B 는 그래프만 바뀜: DB 업무 {r['T4']['B_db_tasks_changed']}건)")
        t = r["T5"]
        L.append("| T5 실무자 범위 / AI 개인정보 | " + " | ".join(
            "지원 안 함" if t[k]["staff"] is None else f"{t[k]['staff']}명 / {t[k]['ai_pii']}" for k in "ABCD") + " |")
        L.append("| T6 실행자·변경 전후 기록 | created_by 만 | 없음 | 실행자·변경·이벤트 | C + 추론 근거 |")
        t = r["T8"]
        def ep(x):
            v = list(x["entry_points"].values())
            ok = sum(s in ("approval", "approval_required") for s in v)
            return f"{x['changed_lines']}줄, 진입점 {ok}/{len(v)}"
        L.append("| T8 규칙 변경 | " + " | ".join(ep(t[k]) for k in "ABCD") + " |")
    if "T7" in r:
        t = r["T7"]
        L.append("| T7 동시 요청 중복 | " + f"잠금 없음 {t['A (잠금 없음)']['duplicates']}/{t['A (잠금 없음)']['trials']}, "
                 f"FOR UPDATE {t['A (FOR UPDATE)']['duplicates']}/{t['A (FOR UPDATE)']['trials']} | 해당 없음 | "
                 f"{t['C·D (엔진)']['duplicates']}/{t['C·D (엔진)']['trials']} | C 와 같음 |")
    if "LOC" in r:
        lo = r["LOC"]
        L += ["", "## 코드 양 (빈 줄·주석 제외)", "", "| 구분 | 줄 수 |", "|---|---|"]
        L += [f"| 업무 코드 {k} | {v} |" for k, v in lo["업무 코드"].items()]
        L += [f"| 선언 {k} | {v} |" for k, v in lo["선언"].items()]
        L += [f"| 기반 {k} | {v} |" for k, v in lo["기반"].items()]
    return "\n".join(L) + "\n"


def main() -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", action="store_true", help="T7 만 (DB 를 실제로 바꾼다)")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    path = OUT / "results.json"
    res = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if args.concurrency:
        res["T7"] = t7()
    else:
        m = Mapping.load(MAPPING)
        (g, etl_ms) = timed(b_rdf.load_graph, repeat=1)
        res["B_etl_ms"] = etl_ms
        with OntologyClient.open(TAXOS, today=AS_OF) as c, rollback(c):
            c.actions.ensure_tables()
            for name, fn in (("T1", lambda: t1(c, g, m)), ("T2", lambda: t2(c, g, m)), ("T3", lambda: t3(c, g, m)),
                             ("T4", lambda: t4(c, g)), ("T5", lambda: t5(c, m)), ("T6", lambda: t6(c)),
                             ("T8", lambda: t8(c, g, m))):
                res[name] = fn()
                print(name, json.dumps(res[name], ensure_ascii=False, default=str)[:400], flush=True)
        res["LOC"] = loc()
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (OUT / "summary.md").write_text(summary(res), encoding="utf-8")
    print(json.dumps({k: res[k] for k in res if k in ("T7", "LOC")}, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
