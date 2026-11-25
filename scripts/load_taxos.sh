#!/bin/bash
# 실행 중인 PostgreSQL 컨테이너에 세무 합성 데이터를 (다시) 적재합니다.
# 데이터를 새로 만들려면 먼저: python3 scripts/generate_taxos_data.py
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose exec -T postgres bash /docker-entrypoint-initdb.d/02-taxos.sh
docker compose exec -T postgres psql -U "${POSTGRES_USER:-onto}" -d taxos -c "
SELECT 'accountants' AS table_name, count(*) FROM accountants
UNION ALL SELECT 'customers', count(*) FROM customers
UNION ALL SELECT 'tax_returns', count(*) FROM tax_returns
UNION ALL SELECT 'document_types', count(*) FROM document_types
UNION ALL SELECT 'document_submissions', count(*) FROM document_submissions
UNION ALL SELECT 'consultations', count(*) FROM consultations
UNION ALL SELECT 'tasks', count(*) FROM tasks
UNION ALL SELECT 'risk_events', count(*) FROM risk_events;"
