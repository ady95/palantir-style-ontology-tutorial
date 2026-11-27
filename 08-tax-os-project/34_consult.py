"""34장. 상담 응대 에이전트 실행과 평가 — 고객 문의 16건 (모든 변경 롤백)

    uv run python 08-tax-os-project/34_consult.py                 # 템플릿 응답 (LLM 없이)
    uv run python 08-tax-os-project/34_consult.py --llm --repeat 3

평가: 처리 경로(답변/넘김)가 기대와 같은가, 답의 사실(미제출 자료·기한)이 맞는가, 문서 검색이 맞는 문서를 찾는가
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402

from consult import Knowledge, LLMResponder, TemplateResponder, facts, handle, open_inquiries  # noqa: E402
from minionto import OntologyClient  # noqa: E402
from minionto.agent import AgentSession  # noqa: E402

TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"
AI = {"id": "agent-01", "role": "AI비서", "type": "agent"}

# 기대 경로 — 업무 규칙(R16·R17)과 33-1 의 역할 분담에서 정한 것. both: 어느 쪽도 허용
EXPECTED = {"Q00001": "answered", "Q00002": "answered", "Q00003": "answered", "Q00004": "answered",
            "Q00005": "answered", "Q00006": "answered", "Q00007": "answered", "Q00008": "escalated",
            "Q00009": "escalated", "Q00010": "escalated", "Q00011": "escalated", "Q00012": "escalated",
            "Q00013": "both", "Q00014": "escalated", "Q00015": "answered", "Q00016": "escalated"}
# 문서 검색 정답 (상위 2개 안에 있어야)
DOC = {"Q00003": "K04", "Q00004": "K01", "Q00005": "K03", "Q00007": "K05", "Q00013": "K07", "Q00015": "K06"}


def fact_ok(iid: str, answer: str, fx: dict) -> bool | None:
    """답에 들어가야 할 사실이 들어갔는가 (검사할 문의만)"""
    cur = fx.get("current_return") or {}
    if iid in ("Q00001", "Q00002"):
        return all(d["doc"] in answer for d in cur.get("not_received", []))
    if iid == "Q00004":
        return "1월 25일" in answer or "2027-01-25" in answer or "01-25" in answer
    if iid == "Q00007":
        return any(w in answer for w in ("필요하지 않", "내지 않아도", "제출하지 않아도", "필요 없"))
    return None


def run_once(c, kb, responder, rep: int) -> list[dict]:
    rows = []
    with c.conn.transaction(force_rollback=True):
        s = AgentSession(c, AI, task_id=f"consult-{rep}", max_calls=10_000, max_rows=100)
        for q in open_inquiries(c):
            t0 = time.perf_counter()
            fx = facts(s, q)
            r = handle(s, kb, responder, q)
            exp = EXPECTED[q["inquiry_id"]]
            rows.append({"id": r.inquiry_id, "category": r.category, "route": r.route, "by": r.by,
                         "route_ok": exp == "both" or r.route == exp,
                         "fact_ok": fact_ok(r.inquiry_id, r.answer, fx) if r.route == "answered" else None,
                         "answer": r.answer, "reason": r.reason, "seconds": round(time.perf_counter() - t0, 2)})
        tasks = c.conn.execute("SELECT count(*) AS n FROM tasks WHERE task_type = '고객연락' AND created_by = 'agent'"
                               ).fetchone()["n"]
        rows.append({"tasks_created": tasks})
    return rows


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")
    use_llm = "--llm" in sys.argv
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    kb = Knowledge()
    hits = sum(DOC[i] in [d.id for d in kb.search(q)] for i, q in (
        ("Q00003", "카드 매출 내역은 어디서 받아서 보내면 돼요?"), ("Q00004", "이번 신고 마감이 언제인가요?"),
        ("Q00005", "자료 요청 받았는데, 매입 세금계산서는 어떻게 뽑나요?"),
        ("Q00007", "간이과세자도 사업용 카드 사용내역을 내야 하나요?"),
        ("Q00013", "자료가 좀 늦어질 것 같은데 기한을 넘기면 어떻게 되나요?"), ("Q00015", "자료는 어떤 방법으로 보내면 되나요?")))
    print(f"문서 {len(kb.docs)}개 · 검색 방식 {kb.mode} · 정답 문서가 상위 2개 안에: {hits}/{len(DOC)}")
    if use_llm:
        from minionto.llm import ChatClient
        responder = LLMResponder(ChatClient())
    else:
        responder = TemplateResponder()
    with OntologyClient.open(TAXOS, today=AS_OF) as c:
        c.actions.ensure_tables()
        for rep in range(1, repeat + 1):
            rows = run_once(c, kb, responder, rep)
            tail = rows.pop()
            print(f"\n## 실행 {rep} ({responder.name})")
            for r in rows:
                mark = "" if r["route_ok"] else "  ← 기대와 다름"
                fact = "" if r["fact_ok"] is None else ("  사실 OK" if r["fact_ok"] else "  사실 틀림")
                text = (r["answer"] or r["reason"])[:90].replace("\n", " ")
                print(f"  {r['id']} {r['category']:4s} {r['route']:9s} ({r['by']:8s}) {text}{fact}{mark}")
            n = len(rows)
            print(f"  경로 일치 {sum(r['route_ok'] for r in rows)}/{n} · 답변 {sum(r['route'] == 'answered' for r in rows)} · "
                  f"넘김 {sum(r['route'] == 'escalated' for r in rows)} {dict(Counter(r['by'] for r in rows if r['route'] == 'escalated'))} · "
                  f"사실 검사 {sum(r['fact_ok'] is True for r in rows)}/{sum(r['fact_ok'] is not None for r in rows)} · "
                  f"고객연락 업무 {tail['tasks_created']}건 · 평균 {sum(r['seconds'] for r in rows) / n:.1f}초")
    if use_llm:
        print(f"\nLLM 사용: {responder.usage}")
    print("\n(모든 변경을 롤백했습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
