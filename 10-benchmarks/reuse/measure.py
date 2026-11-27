"""35장. 재사용성 측정 — 코드 비중, 엔진 안의 도메인 흔적, 장별 변경량

    uv run python 10-benchmarks/reuse/measure.py            # 표 출력 (DB 불필요)
    uv run python 10-benchmarks/reuse/measure.py --json     # results/measure.json 에도 저장

1. 코드 비중   — 범용 엔진 / 도메인 정의(YAML) / 도메인 코드(Function·연동) / 데이터 스키마 / 도메인 응용
                 줄 수는 빈 줄과 주석만 있는 줄을 뺀 것
2. 도메인 흔적 — 엔진 모듈의 '실행되는 코드'(문서 문자열·주석 제외)에 도메인 이름이 나오는 곳 (AST 로 찾는다)
3. 변경 이력   — 15장 이후 장별 커밋에서 엔진과 도메인 쪽이 각각 몇 줄 바뀌었나 (git numstat)
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ENGINE = ROOT / "03-ontology-engine"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(HERE))

from minionto.loader import load  # noqa: E402
from smoke import DOMAINS, path_of, vocabulary  # noqa: E402

APPS = {"taxos": ["08-tax-os-project", "06-hybrid-ontology"], "northwind": ["07-northwind-project"],
        "maintenance": []}
SCHEMA = {"taxos": ["datasets/taxos/schema.sql", "datasets/taxos/migrations"],
          "northwind": ["datasets/northwind/migrations"],          # 공개 샘플(northwind.sql)은 이미 있던 데이터
          "maintenance": ["datasets/maintenance/schema.sql"]}


def code_lines(path: Path) -> int:
    """빈 줄과 주석만 있는 줄을 뺀 줄 수 (py·yaml·ttl: #, sql: --)"""
    mark = "--" if path.suffix == ".sql" else "#"
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith(mark):
            n += 1
    return n


def files(paths: list[str], exts=(".py", ".yaml", ".sql", ".ttl")) -> list[Path]:
    out = []
    for p in paths:
        q = ROOT / p
        if q.is_file():
            out.append(q)
        elif q.is_dir():
            out += [f for f in sorted(q.rglob("*")) if f.suffix in exts and "__pycache__" not in f.parts
                    and "results" not in f.parts]
    return out


# ---------------------------------------------------------------------- 1. 코드 비중
def code_share() -> dict:
    eng = {f.name: code_lines(f) for f in sorted((ENGINE / "minionto").glob("*.py"))}
    dom = {}
    for d in DOMAINS:
        odir = ENGINE / "ontologies" / d
        dom[d] = {
            "정의(YAML)": code_lines(odir / "ontology.yaml"),
            "도메인 코드": sum(code_lines(f) for f in odir.glob("*.py")),
            "데이터 스키마": sum(code_lines(f) for f in files(SCHEMA[d], (".sql",))),
            "응용": sum(code_lines(f) for f in files(APPS[d], (".py", ".yaml", ".ttl"))),
        }
    return {"engine": eng, "domains": dom}


# ---------------------------------------------------------------------- 2. 도메인 흔적
def docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def code_texts(path: Path) -> list[tuple[int, str]]:
    """실행되는 코드의 문자열 상수와 이름 (문서 문자열·주석 제외)"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = docstring_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.Name):
            out.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            out.append((node.lineno, node.attr))
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.append((node.lineno, node.name))
    return out


def domain_terms() -> dict[str, set[str]]:
    terms = {}
    for d in DOMAINS:
        o = load(path_of(d))
        t = vocabulary(o) | {ot.display_name for ot in o.object_types.values() if ot.display_name} | set(o.roles)
        terms[d] = {w for w in t if len(w) > 1 and w not in ("AI비서", "시스템")}
    common = set.intersection(*terms.values())
    return {d: t - common for d, t in terms.items()}


def find(term: str, text: str) -> bool:
    if term.isascii():
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text) is not None
    return term in text


def traces() -> dict:
    """모듈마다 도메인 이름이 나오는 줄 수와 이름 (도메인별)"""
    terms = domain_terms()
    out = {}
    for f in sorted((ENGINE / "minionto").glob("*.py")):
        hits, lines = defaultdict(set), set()
        for lineno, text in code_texts(f):
            for d, ts in terms.items():
                for t in ts:
                    if find(t, text):
                        hits[d].add(t)
                        lines.add(lineno)
        if hits:
            out[f.name] = {"lines": len(lines), "terms": {d: sorted(v) for d, v in hits.items()}}
    return out


# ---------------------------------------------------------------------- 3. 변경 이력
def category(path: str) -> str | None:
    if path.startswith("03-ontology-engine/minionto/"):
        return "엔진"
    if path.startswith("03-ontology-engine/ontologies/") or path.startswith("datasets/") \
            or path.startswith(("06-", "07-", "08-")):
        return "도메인"
    if path.startswith(("tests/", "03-ontology-engine/examples/", "10-benchmarks/")):
        return "테스트·예제·실험"
    return None


def history() -> list[dict]:
    log = subprocess.run(["git", "-c", "core.quotepath=off", "log", "--reverse", "--format=%h%x09%s", "part4..HEAD"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    rows = []
    for line in log.splitlines():
        h, subject = line.split("\t", 1)
        m = re.search(r"ch\.(\d+)", subject)
        if not m:
            continue
        stat = subprocess.run(["git", "-c", "core.quotepath=off", "show", "--numstat", "--format=", h], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
        sums = defaultdict(int)
        for s in stat.splitlines():
            a, dl, p = s.split("\t", 2)
            if a == "-" or p.endswith((".csv", ".txt", ".jsonl", ".json", ".md")):
                continue
            c = category(p)
            if c:
                sums[c] += int(a) + int(dl)
        rows.append({"chapter": int(m.group(1)), "commit": h, "subject": subject[:70], **sums})
    return rows


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    share, tr, hist = code_share(), traces(), history()

    print("1. 코드 비중 (빈 줄·주석 제외 줄 수)")
    total_engine = sum(share["engine"].values())
    print(f"  범용 엔진 minionto/: {total_engine:,}줄 ({len(share['engine'])}개 모듈)")
    print(f"  {'도메인':12s} {'정의(YAML)':>10s} {'도메인 코드':>10s} {'데이터 스키마':>12s} {'응용':>8s}")
    for d, v in share["domains"].items():
        print(f"  {d:12s} {v['정의(YAML)']:10,d} {v['도메인 코드']:10,d} {v['데이터 스키마']:12,d} {v['응용']:8,d}")

    print("\n2. 엔진 안의 도메인 흔적 (실행되는 코드만, 문서 문자열·주석 제외)")
    for mod, v in tr.items():
        names = "; ".join(f"{d}: {', '.join(t[:6])}" for d, t in v["terms"].items())
        print(f"  {mod:16s} {v['lines']:3d}줄  {names}")
    print(f"  합계 {len(tr)}개 모듈 {sum(v['lines'] for v in tr.values())}줄")

    print("\n3. 장별 변경량 (추가+삭제 줄, 데이터·결과·문서 파일 제외)")
    print(f"  {'장':>3s} {'커밋':8s} {'엔진':>6s} {'도메인':>6s} {'테스트·예제·실험':>14s}")
    for r in hist:
        print(f"  {r['chapter']:3d} {r['commit']:8s} {r.get('엔진', 0):6,d} {r.get('도메인', 0):6,d} "
              f"{r.get('테스트·예제·실험', 0):14,d}  {r['subject'][:44]}")

    if "--json" in sys.argv:
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / "measure.json").write_text(
            json.dumps({"share": share, "traces": tr, "history": hist}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
