#!/bin/bash
# 실행 중인 PostgreSQL 컨테이너에 Northwind 를 (다시) 적재합니다. 실습용 추가 테이블(migrations)도 함께.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose exec -T postgres bash /docker-entrypoint-initdb.d/01-northwind.sh
bash scripts/check_db.sh
