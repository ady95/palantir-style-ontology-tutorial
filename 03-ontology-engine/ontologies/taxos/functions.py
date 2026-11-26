"""세무 온톨로지 Function 구현 (18장)

계약(입력·출력 타입)은 ontology.yaml 의 functions 에 있고, 여기에는 계산만 있다.
모든 함수의 첫 인자 ctx 는 FunctionContext — 온톨로지 조회(ctx.objects)와 기준일(ctx.today)을 준다.
테이블·컬럼 이름 대신 객체·링크 이름만 쓴다는 점에 주목하자.
"""

from __future__ import annotations

OPEN_DOCS = ["미요청", "요청됨"]

# 과거 위험 이벤트의 가중치 (실습용 규칙 — 실제 업무에서는 세무사가 정한다)
EVENT_WEIGHTS = {"기한후신고": 15, "가산세부과": 10, "연락두절": 20, "자료지연제출": 5}


def missing_documents(ctx, tax_return):
    """신고에서 아직 제출되지 않은(미요청·요청됨) 자료 목록"""
    return tax_return.submissions.where(status=OPEN_DOCS).order_by("doc_type_code").all()


def filing_delay_risk(ctx, customer):
    """미제출 자료 수 + 남은 기한 + 과거 위험 이벤트 → 신고 지연 위험도 (0~100)"""
    score, reasons = 0, []

    open_returns = customer.tax_returns.where(status="자료수집중").where(
        {"field": "due_date", "op": "gte", "value": "$today"}).order_by("due_date").all()
    missing = sum(r.submissions.where(status=OPEN_DOCS).count() for r in open_returns)
    if missing:
        score += min(missing * 8, 40)
        reasons.append(f"미제출 자료 {missing}건")
        days = (open_returns[0].due_date - ctx.today).days
        bonus = 30 if days <= 3 else 20 if days <= 7 else 10 if days <= 14 else 0
        if bonus:
            score += bonus
            reasons.append(f"신고 기한 {days}일 남음")

    event_score = 0
    for row in customer.risk_events.count_by("event_type"):
        event_score += EVENT_WEIGHTS.get(row["event_type"], 0) * row["count"]
        reasons.append(f"과거 {row['event_type']} {row['count']}회")
    score += min(event_score, 30)

    score = min(score, 100)
    level = "높음" if score >= 60 else "보통" if score >= 30 else "낮음"
    return {"score": score, "level": level, "reasons": reasons}


def has_capacity(ctx, accountant):
    """담당 고객 수가 최대치(max_workload) 미만인가"""
    return accountant.customers.count() < accountant.max_workload


def recommend_assignee(ctx, customer):
    """현재 담당자와 같은 팀의 실무자 중 여유(최대치 - 담당 수)가 가장 큰 사람. 없으면 None"""
    current = customer.accountant
    workload = {r["accountant_id"]: r["count"] for r in ctx.objects("Customer").count_by("accountant_id")}
    candidates = ctx.objects("Accountant").where(role="실무자", team=current.team).all()
    best, best_room = None, 0
    for acc in candidates:
        room = acc.max_workload - workload.get(acc.pk, 0)
        if acc != current and room > best_room:
            best, best_room = acc, room
    return best
