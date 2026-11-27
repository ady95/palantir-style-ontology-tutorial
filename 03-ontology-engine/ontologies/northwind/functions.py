"""Northwind 온톨로지 Function 구현 (18장)"""

from __future__ import annotations

from decimal import Decimal


def order_total(ctx, order):
    """주문 상세(단가 × 수량 × (1 - 할인))의 합계

    수량·단가·할인은 주문-상품 '링크의 속성'이다. MiniOnto v0.1 은 링크 속성을 지원하지 않으므로
    ctx.sql 탈출구를 쓴다. 온톨로지 메타모델의 한계가 어디서 드러나는지 보여 주는 예.
    """
    row = ctx.sql("""SELECT COALESCE(SUM(unit_price * quantity * (1 - discount)), 0) AS total
                     FROM order_details WHERE order_id = %s""", (order.pk,))[0]
    return Decimal(str(row["total"])).quantize(Decimal("0.01"))


def is_low_stock(ctx, product):
    """판매 중이고, 재고 + 입고 예정이 재주문 기준 이하인가"""
    if product.discontinued:
        return False
    return (product.units_in_stock or 0) + (product.units_on_order or 0) <= (product.reorder_level or 0)


# ---------------------------------------------------------------------- 32장: 발주
def reorder_quantity(ctx, product):
    """재고와 입고 예정을 합쳐 재주문 기준의 두 배(최소 10)까지 채우는 수량"""
    have = (product.units_in_stock or 0) + (product.units_on_order or 0)
    return max(max((product.reorder_level or 0) * 2, 10) - have, 0)


def purchase_amount(ctx, product, quantity):
    """단가 × 수량 (실습용: 판매 단가를 매입 단가로 본다)"""
    return (Decimal(str(product.unit_price or 0)) * quantity).quantize(Decimal("0.01"))


def purchase_amount_at_least(ctx, product, quantity, limit):
    """발주 금액이 limit 이상인가 (금액별 승인 조건)"""
    return purchase_amount(ctx, product, quantity) >= Decimal(str(limit))


def stock_after(ctx, product, quantity, sign=1):
    """재고를 quantity × sign 만큼 바꾼 값 (입고 +1, 출고 -1)"""
    return (product.units_in_stock or 0) + quantity * sign


def has_stock(ctx, product, quantity):
    return (product.units_in_stock or 0) >= quantity


def on_order_after(ctx, product, quantity, sign=1):
    """입고 예정을 quantity × sign 만큼 바꾼 값 (0 아래로 내려가지 않음)"""
    return max((product.units_on_order or 0) + quantity * sign, 0)
