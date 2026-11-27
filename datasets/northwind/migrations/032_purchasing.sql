-- 32장: 발주(구매 주문) 실습 테이블 — 공개 샘플 Northwind 에는 없어 추가한다.
-- 발주는 Action(request_purchase)으로만 만들고, 입고(receive_purchase)가 상품 재고에 반영한다.
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id             varchar(12)   PRIMARY KEY,
    product_id        smallint      NOT NULL REFERENCES products(product_id),
    supplier_id       smallint      REFERENCES suppliers(supplier_id),
    quantity          integer       NOT NULL CHECK (quantity > 0),
    unit_cost         numeric(10,2) NOT NULL CHECK (unit_cost >= 0),   -- 실습용: 판매 단가를 매입 단가로 본다
    amount            numeric(12,2) NOT NULL,
    status            varchar(10)   NOT NULL CHECK (status IN ('발주완료', '입고완료', '취소')),
    requested_by      varchar(20)   NOT NULL,
    ordered_at        timestamp     NOT NULL,
    received_at       timestamp,
    received_quantity integer       CHECK (received_quantity >= 0),
    note              text,
    CHECK ((status = '입고완료') = (received_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS purchase_orders_product ON purchase_orders (product_id);
