#!/bin/bash
# 실행 중인 PostgreSQL 컨테이너에 설비 정비 합성 데이터(35장)를 (다시) 적재합니다.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose exec -T postgres bash /docker-entrypoint-initdb.d/03-maintenance.sh
docker compose exec -T postgres psql -U "${POSTGRES_USER:-onto}" -d maintenance -c "
SELECT 'technicians' AS table_name, count(*) FROM technicians
UNION ALL SELECT 'equipment', count(*) FROM equipment
UNION ALL SELECT 'work_orders', count(*) FROM work_orders;"
