"""28장. 비교 실험 결과 집계 → results/<파일>_summary.md

    uv run python 10-benchmarks/agent_comparison/report.py [results/runs.jsonl]
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scenarios import BY_ID  # noqa: E402

AGENTS = ["sql", "plain", "ontology", "graph", "sql_stale", "plain_stale"]
LABEL = {"sql": "A. SQL 직접", "plain": "B. 일반 도구", "ontology": "C. 온톨로지 도구", "graph": "D. 업무 그래프",
         "sql_stale": "A'. SQL (옛 프롬프트)", "plain_stale": "B'. 일반 도구 (옛 프롬프트)"}


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[max(0, math.ceil(0.95 * len(xs)) - 1)] if xs else 0.0


def rate(rows: list[dict]) -> str:
    return f"{sum(r['ok'] for r in rows)}/{len(rows)}" if rows else "-"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "results" / "runs.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    by = defaultdict(list)
    for r in rows:
        by[(r["agent"], r["scenario"])].append(r)
    agents = [a for a in AGENTS if any(r["agent"] == a for r in rows)]
    lines = ["## 시나리오별 (성공·차단 / 반복)", "", "| 과제 | 종류 | " + " | ".join(LABEL[a] for a in agents) + " |",
             "|---|---|" + "---|" * len(agents)]
    for sid, sc in BY_ID.items():
        if not any((a, sid) in by for a in agents):
            continue
        lines.append(f"| {sid} {sc.title} | {sc.kind} | " + " | ".join(rate(by[(a, sid)]) for a in agents) + " |")
    lines += ["", "## 방식별 합계", "", "| 지표 | " + " | ".join(LABEL[a] for a in agents) + " |",
              "|---|" + "---|" * len(agents)]

    def kind(a, ks):
        return [r for r in rows if r["agent"] == a and r["kind"] in ks]
    metrics = [
        ("조회 정확도", lambda a: rate(kind(a, ["read"]))),
        ("정상 업무 성공", lambda a: rate(kind(a, ["task"]))),
        ("비정상 요청 차단", lambda a: rate(kind(a, ["block"]))),
        ("중복 실행 방지", lambda a: rate(kind(a, ["duplicate"]))),
        ("개인정보 비노출", lambda a: rate(kind(a, ["privacy"]))),
        ("압박·간접 요청 차단", lambda a: rate(kind(a, ["pressure"]))),
        ("일괄 업무 완수", lambda a: rate(kind(a, ["bulk"]))),
        ("일괄 업무 규칙 위반 (건, 합계)", lambda a: str(sum(int(re.search(r"규칙 위반 (\d+)", r["note"]).group(1))
                                                     for r in kind(a, ["bulk"]) if "규칙 위반" in r["note"]))),
    ]
    for name, f in metrics:
        lines.append(f"| {name} | " + " | ".join(f(a) for a in agents) + " |")
    for a_name, f in [
        ("감사 기록 완전성 (바뀐 행 중 실행 기록에 남은 비율)", lambda rs: _avg([r["audit_coverage"] for r in rs
                                                                  if r["audit_coverage"] is not None], 3)),
        ("도구 결과로 LLM 에 간 개인정보 (값 개수 합계)", lambda rs: str(sum(r.get("pii_sent", 0) for r in rs))),
        ("개인정보가 LLM 에 간 실행 수", lambda rs: f"{sum(r.get('pii_sent', 0) > 0 for r in rs)}/{len(rs)}"),
        ("실행당 LLM 호출", lambda rs: _avg([r["usage"].get("llm_calls", 0) for r in rs], 1)),
        ("실행당 도구 호출", lambda rs: _avg([len(r["calls"]) for r in rs], 1)),
        ("실행당 입력 토큰", lambda rs: _avg([r["usage"].get("prompt_tokens", 0) for r in rs], 0)),
        ("실행당 출력 토큰", lambda rs: _avg([r["usage"].get("completion_tokens", 0) for r in rs], 0)),
        ("응답 시간 평균(초)", lambda rs: _avg([r["seconds"] for r in rs if r["seconds"]], 1)),
        ("응답 시간 P95(초)", lambda rs: f"{p95([r['seconds'] for r in rs if r['seconds']]):.1f}"),
        ("실행 오류·단계 초과", lambda rs: str(sum(bool(r["error"]) or r["stopped"] for r in rs))),
    ]:
        lines.append(f"| {a_name} | " + " | ".join(f([r for r in rows if r["agent"] == a]) for a in agents) + " |")
    lines += ["", "## 실패한 실행", ""]
    for r in rows:
        if not r["ok"]:
            lines.append(f"- {r['scenario']} {LABEL[r['agent']]} #{r['rep']}: {r['note']} · 변경 {r['changed']}")
    text = "\n".join(lines) + "\n"
    path.with_name(path.stem + "_summary.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def _avg(xs: list, nd: int) -> str:
    if not xs:
        return "-"
    v = sum(xs) / len(xs)
    return f"{v:.{nd}f}" if nd else f"{v:,.0f}"


if __name__ == "__main__":
    raise SystemExit(main())
