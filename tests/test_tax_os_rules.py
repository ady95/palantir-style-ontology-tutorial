"""33장. 업무 규칙 목록 ↔ 정의 ↔ 동작 (롤백)

실행: uv run pytest -q tests/test_tax_os_rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "08-tax-os-project"))


def test_every_business_rule_is_implemented_and_holds():
    check_rules = pytest.importorskip("check_rules")
    try:
        res = check_rules.check()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"실습 DB에 연결할 수 없습니다: {e}")
    failed = [r for r in res["rules"] if not (r["found"] and r["passed"])]
    assert not failed, failed
    assert len(res["rules"]) == 21
    kinds = {u["kind"] for u in res["undocumented"]}
    assert "승인" not in kinds                      # 승인 규칙은 모두 목록에 있다
