"""PART 2 실습 공통 모듈: 경로, 네임스페이스, 그래프 로딩"""

from __future__ import annotations

import sys
from pathlib import Path

from rdflib import Graph, Namespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / "datasets" / "taxos" / "data"
ONTOLOGY_FILE = HERE / "ontology" / "taxos.ttl"
SHAPES_FILE = HERE / "shapes" / "taxos_shapes.ttl"
OUTPUT_DIR = HERE / "output"
DATA_GRAPH_FILE = OUTPUT_DIR / "taxos_data.ttl"

TX = Namespace("https://example.org/taxos#")          # 스키마(TBox)
TXD = Namespace("https://example.org/taxos/data/")    # 인스턴스(ABox)

# 기준 시나리오 (datasets/taxos/README.md)
AS_OF = "2027-01-18"
CURRENT_DUE = "2027-01-25"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def bind(g: Graph) -> Graph:
    g.bind("tx", TX)
    g.bind("txd", TXD)
    return g


def load_ontology() -> Graph:
    return bind(Graph()).parse(ONTOLOGY_FILE)


def load_data() -> Graph:
    """01_build_graph.py 가 만든 인스턴스 그래프. 없으면 먼저 생성한다."""
    if not DATA_GRAPH_FILE.exists():
        from importlib import import_module
        import_module("01_build_graph").main()
    return bind(Graph()).parse(DATA_GRAPH_FILE)


def load_all() -> Graph:
    """스키마 + 인스턴스"""
    g = load_ontology()
    g += load_data()
    return g


def short(value) -> str:
    """IRI는 tx:Customer 처럼 줄여서 표시"""
    s = str(value)
    for prefix, ns in (("tx:", TX), ("txd:", TXD)):
        if s.startswith(str(ns)):
            return prefix + s[len(str(ns)):]
    return s


def print_rows(title: str, rows, limit: int = 10) -> None:
    rows = list(rows)
    print(f"\n## {title}  ({len(rows)}건)")
    for row in rows[:limit]:
        print("  " + " | ".join(short(v) for v in row))
    if len(rows) > limit:
        print(f"  ... 외 {len(rows) - limit}건")
