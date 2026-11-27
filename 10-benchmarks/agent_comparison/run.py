"""28장. 일반 에이전트와 온톨로지 기반 에이전트 비교 실험

    uv run python 10-benchmarks/agent_comparison/run.py --repeat 3                 # 전체 (19과제 × 3방식 × 3회)
    uv run python 10-benchmarks/agent_comparison/run.py --agents graph --repeat 3  # 일괄 업무(B1)를 업무 그래프로
    uv run python 10-benchmarks/agent_comparison/run.py --agents sql --only N8    # 일부만
    uv run python 10-benchmarks/agent_comparison/report.py [results/stale.jsonl]   # 집계 → results/*_summary.md
    uv run python 10-benchmarks/agent_comparison/run.py --agents sql_stale,plain_stale --only N3,N4,N6,P2,P3,B1 --repeat 3 \\
        --out 10-benchmarks/agent_comparison/results/stale.jsonl                     # 옛 프롬프트 (규칙 세 개 빠짐)

실행마다 바깥 트랜잭션을 열고 끝나면 롤백한다 — 에이전트가 무엇을 바꾸든 DB 는 그대로다.
결과는 results/runs.jsonl 에 한 줄씩 (답의 개인정보 원문은 가린다).
LLM 은 .env 의 OpenAI 호환 API (OPENAI_BASE_URL · OPENAI_API_KEY · OPENAI_MODEL).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "03-ontology-engine"))
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402

from agents import AGENTS, run_turns  # noqa: E402
from minionto import OntologyClient  # noqa: E402
from scenarios import (BY_ID, SCENARIOS, audit_coverage, diff, executions_since, pii_sent, pii_values,  # noqa: E402
                       redact, snapshot)

PII: set[str] = set()
TAXOS = ROOT / "03-ontology-engine" / "ontologies" / "taxos" / "ontology.yaml"
AS_OF = "2027-01-18"


def run_graph(c, chat, run_id: str) -> dict:
    """D. 27장 업무 그래프 (B1 전용) — 자료수집중 신고 전부를 대상으로(min_score=0), 메모는 LLM, 승인 대기에서 멈춘다"""
    import time

    from langgraph.checkpoint.memory import InMemorySaver
    from minionto.workflow import FilingRiskWorkflow, LLMWriter, config
    w = LLMWriter(chat)
    t0 = time.perf_counter()
    g = FilingRiskWorkflow(c, {"id": "agent-01", "role": "AI비서", "type": "agent"}, writer=w,
                           min_score=0).build(checkpointer=InMemorySaver())
    out = g.invoke({"task_id": run_id, "as_of": AS_OF}, config(run_id))
    st = {}
    for it in out["items"]:
        st[it["status"]] = st.get(it["status"], 0) + 1
    calls = [{"tool": x["node"], "result": x.get("status") or x.get("check") or "ok"} for x in out["log"]
             if x["node"] in ("analyze", "act")]
    return {"answers": [json.dumps(st, ensure_ascii=False)], "calls": calls,
            "usage": {"prompt_tokens": w.usage["prompt_tokens"], "completion_tokens": w.usage["completion_tokens"],
                      "llm_calls": w.usage["calls"]},
            "seconds": round(time.perf_counter() - t0, 2), "stopped": False}


def run_one(c, chat, agent_name: str, sc, rep: int) -> dict:
    run_id = f"{agent_name}-{sc.id}-{rep}"
    with c.conn.transaction(force_rollback=True):
        if sc.setup:
            sc.setup(c)
        truth = sc.truth(c)
        known = {str(r["execution_id"]) for r in c.conn.execute("SELECT execution_id FROM minionto.action_log")}
        before = snapshot(c.conn)
        try:
            if agent_name == "graph":
                res = run_graph(c, chat, run_id)
            else:
                res = run_turns(chat, AGENTS[agent_name](c, AS_OF, run_id), sc.turns, max_steps=sc.max_steps)
            error = None
        except Exception as e:  # noqa: BLE001 — LLM 장애도 결과로 남긴다
            res = {"answers": [], "calls": [], "usage": {}, "seconds": None, "stopped": True}
            error = f"{type(e).__name__}: {str(e)[:200]}"
        d = diff(before, snapshot(c.conn))
        execs = executions_since(c.conn, known)
        ok, note = sc.check(res, d, truth) if error is None else (False, "실행 오류")
        approvals = c.conn.execute("SELECT count(*) AS n FROM minionto.approval_requests WHERE status = 'pending'"
                                   ).fetchone()["n"]
    secrets = truth if sc.kind == "privacy" else []
    return {"agent": agent_name, "scenario": sc.id, "kind": sc.kind, "rep": rep, "ok": ok, "note": note,
            "error": error, "changes": len(d), "changed": sorted({f"{x['table']}.{x['op']}" for x in d}),
            "audit_coverage": audit_coverage(d, execs),
            "executions": [{"action": e["action"], "status": e["status"]} for e in execs],
            "pending_approvals": approvals, "pii_sent": pii_sent(res["calls"], PII),
            "calls": [{"tool": x["tool"], "result": x["result"]} for x in res["calls"]],
            "sql": [redact(x["args"].get("sql", ""), secrets) for x in res["calls"] if x["tool"] == "run_sql"],
            "answers": [redact(a, secrets) for a in res["answers"]],
            "usage": res["usage"], "seconds": res["seconds"], "stopped": res["stopped"]}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="sql,plain,ontology")
    ap.add_argument("--only", help="쉼표 구분 시나리오 ID")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default=str(HERE / "results" / "runs.jsonl"))
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    from minionto.llm import ChatClient
    chat = ChatClient()
    scs = [BY_ID[s] for s in args.only.split(",")] if args.only else SCENARIOS
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with OntologyClient.open(TAXOS, today=AS_OF) as c, out.open("a" if args.append else "w", encoding="utf-8") as f:
        c.actions.ensure_tables()
        global PII
        PII = pii_values(c.conn)
        for rep in range(1, args.repeat + 1):
            for sc in scs:
                for a in args.agents.split(","):
                    if a == "graph" and sc.kind != "bulk":
                        continue                                  # 업무 그래프는 일괄 업무에만
                    try:
                        row = run_one(c, chat, a, sc, rep)
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
                        continue
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                    f.flush()
                    tok = row["usage"].get("prompt_tokens", 0) + row["usage"].get("completion_tokens", 0)
                    print(f"{rep} {sc.id:3s} {a:9s} {'OK ' if row['ok'] else 'NG '} {row['note'][:60]:60s} "
                          f"calls {len(row['calls']):2d} tok {tok:6d} {row['seconds']}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
