"""설비 정비 온톨로지 Function 구현 (35장)"""

from __future__ import annotations


def days_since_inspection(ctx, equipment):
    """마지막 점검 뒤 지난 일수 (기준일은 엔진의 today)"""
    return (ctx.today - equipment.last_inspected).days


def inspection_due(ctx, equipment):
    """점검 주기가 지났는가"""
    return days_since_inspection(ctx, equipment) >= equipment.inspection_cycle_days


def has_capacity(ctx, technician):
    """진행 중 작업(배정·작업중)이 동시 작업 한도보다 적은가"""
    open_orders = technician.work_orders.where(status=["배정", "작업중"]).count()
    return open_orders < technician.max_open_orders


def same_line(ctx, work_order, technician):
    """작업 지시의 설비와 정비원이 같은 라인인가"""
    return work_order.equipment.line == technician.line
