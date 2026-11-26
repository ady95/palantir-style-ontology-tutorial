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
