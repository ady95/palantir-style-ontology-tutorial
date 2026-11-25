#!/bin/bash
# 실습 DB 상태 확인: Northwind 주요 테이블 행 수
set -euo pipefail
cd "$(dirname "$0")/.."
U="${POSTGRES_USER:-onto}"

docker compose exec -T postgres psql -U "$U" -d northwind -c "
SELECT 'customers' AS table_name, count(*) FROM customers
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'order_details', count(*) FROM order_details
UNION ALL SELECT 'products', count(*) FROM products
UNION ALL SELECT 'suppliers', count(*) FROM suppliers
UNION ALL SELECT 'employees', count(*) FROM employees
UNION ALL SELECT 'categories', count(*) FROM categories
UNION ALL SELECT 'shippers', count(*) FROM shippers;"
