#!/bin/bash
# maintenance 데이터베이스(설비 정비 합성 데이터, 35장) 생성 및 적재
# - 컨테이너 최초 기동 시 자동 실행
# - 이미 실행 중인 컨테이너에서는 scripts/load_maintenance.sh 로 다시 적재 (기존 maintenance DB는 지우고 새로 만듦)
set -euo pipefail

DATA=/datasets/maintenance
U="$POSTGRES_USER"

psql -v ON_ERROR_STOP=1 -q -U "$U" -d postgres -c "DROP DATABASE IF EXISTS maintenance WITH (FORCE);"
psql -v ON_ERROR_STOP=1 -q -U "$U" -d postgres -c "CREATE DATABASE maintenance OWNER \"$U\";"
psql -v ON_ERROR_STOP=1 -q -U "$U" -d maintenance -f "$DATA/schema.sql"
echo "[initdb] maintenance loaded"
