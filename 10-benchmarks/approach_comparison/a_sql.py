"""A. 일반 DB·API — SQL 과 손으로 쓴 서비스 함수

요구사항을 빠짐없이 구현한 '성실한 개발자' 버전이다. 규칙·권한은 함수 안의 코드로,
승인 요청·감사 기록을 위한 별도 기반은 없다 (필요하면 직접 만들어야 한다).
"""

from __future__ import annotations

import psycopg

PII = ("representative", "phone", "business_reg_no")


# ---------------------------------------------------------------------- T1 조회
def missing_customers(conn, as_of: str, due: str) -> list[str]:
    """기한(as_of~due) 안의 자료수집중 신고에 미제출(미요청·요청됨) 자료가 남은 영업 고객"""
    return [r["customer_id"] for r in conn.execute(
        """SELECT DISTINCT c.customer_id FROM customers c
           JOIN tax_returns r ON r.customer_id = c.customer_id
           JOIN document_submissions s ON s.return_id = r.return_id
           WHERE c.status = '영업' AND r.status = '자료수집중' AND r.due_date BETWEEN %s AND %s
             AND s.status IN ('미요청', '요청됨')
           ORDER BY 1""", (as_of, due)).fetchall()]


# ---------------------------------------------------------------------- T2 분류
def repeat_risk(conn) -> list[str]:
    """기한후신고와 가산세부과 이력이 모두 있는 고객"""
    return [r["customer_id"] for r in conn.execute(
        """SELECT c.customer_id FROM customers c
           WHERE EXISTS (SELECT 1 FROM risk_events e WHERE e.customer_id = c.customer_id AND e.event_type = '기한후신고')
             AND EXISTS (SELECT 1 FROM risk_events e WHERE e.customer_id = c.customer_id AND e.event_type = '가산세부과')
           ORDER BY 1""").fetchall()]


# ---------------------------------------------------------------------- T4 업무 실행
def request_documents(conn, return_id: str, actor: dict, note: str | None = None, lock: bool = False,
                      rule_v2: bool = False) -> dict:
    """누락 자료 요청. 실행 조건·승인 판단을 코드로. lock=True 면 신고 행을 잠근다 (동시 실행 대비)
    rule_v2: T8 의 규칙 변경 (기한후신고 이력 고객의 AI 요청도 승인) — 바뀐 줄은 아래 '# v2' 표시 3줄"""
    with conn.transaction():
        r = conn.execute("SELECT r.*, c.status AS business_status, c.risk_level, c.accountant_id "
                         "FROM tax_returns r JOIN customers c USING (customer_id) WHERE r.return_id = %s"
                         + (" FOR UPDATE OF r" if lock else ""), (return_id,)).fetchone()
        if r is None:
            return {"status": "blocked", "reason": "없는 신고"}
        if actor["role"] not in ("세무사", "실무자", "AI비서"):
            return {"status": "blocked", "reason": "권한 없음"}
        if actor["role"] == "실무자" and r["accountant_id"] != actor["id"]:
            return {"status": "blocked", "reason": "담당 고객이 아님"}
        if r["status"] != "자료수집중":
            return {"status": "blocked", "reason": "자료수집중인 신고가 아님"}
        if r["business_status"] != "영업":
            return {"status": "blocked", "reason": "휴업·폐업 고객"}
        if conn.execute("SELECT 1 FROM tasks WHERE return_id = %s AND task_type = '자료요청' "
                        "AND status IN ('대기', '진행중')", (return_id,)).fetchone():
            return {"status": "blocked", "reason": "진행 중인 자료요청 업무"}
        if not conn.execute("SELECT 1 FROM document_submissions WHERE return_id = %s AND status = '미요청'",
                            (return_id,)).fetchone():
            return {"status": "blocked", "reason": "미요청 자료 없음"}
        if r["risk_level"] == "높음" and actor["type"] == "agent":
            return {"status": "approval", "reason": "고위험 고객 — 세무사 승인 필요 (승인 요청 기록 기능 없음)"}
        if rule_v2 and actor["type"] == "agent" and conn.execute(                                  # v2
                "SELECT 1 FROM risk_events WHERE customer_id = %s AND event_type = '기한후신고'",    # v2
                (r["customer_id"],)).fetchone():                                                    # v2
            return {"status": "approval", "reason": "기한후신고 이력 고객 — 세무사 승인 필요"}
        conn.execute("UPDATE document_submissions SET status = '요청됨', requested_at = %s "
                     "WHERE return_id = %s AND status = '미요청'", (_today(conn), return_id))
        n = conn.execute("SELECT coalesce(max(substr(task_id, 2)::int), 0) + 1 AS n FROM tasks").fetchone()["n"]
        conn.execute("INSERT INTO tasks (task_id, customer_id, return_id, task_type, assignee_id, status, due_date, "
                     "created_by, created_at, note) VALUES (%s, %s, %s, '자료요청', %s, '진행중', %s, %s, now(), %s)",
                     (f"T{n:05d}", r["customer_id"], return_id, r["accountant_id"], r["due_date"], actor["type"], note))
        return {"status": "executed", "task_id": f"T{n:05d}"}


def _today(conn) -> str:
    return getattr(conn, "as_of", None) or "2027-01-18"


# ---------------------------------------------------------------------- T5 권한
def list_customers(conn, actor: dict) -> list[dict]:
    """조회 API: 실무자는 담당 고객만, AI 에이전트에게는 개인정보를 빼고"""
    sql, args = "SELECT * FROM customers", []
    if actor["role"] == "실무자":
        sql, args = sql + " WHERE accountant_id = %s", [actor["id"]]
    rows = conn.execute(sql + " ORDER BY customer_id", args).fetchall()
    if actor["type"] == "agent":
        rows = [{k: v for k, v in r.items() if k not in PII} for r in rows]
    return rows


# ---------------------------------------------------------------------- T6 감사
def who_changed(conn, return_id: str) -> list[dict]:
    """무엇이 남는가: tasks.created_by (human/agent/system) 뿐 — 실행자 ID·변경 전후 값은 없다"""
    return conn.execute("SELECT task_id, created_by, created_at FROM tasks WHERE return_id = %s "
                        "AND task_type = '자료요청' ORDER BY task_id", (return_id,)).fetchall()


def connect(url: str):
    from psycopg.rows import dict_row
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)
