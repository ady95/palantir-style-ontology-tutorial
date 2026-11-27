#!/bin/bash
# taxos 데이터베이스(세무 합성 데이터) 생성 및 적재
# - 컨테이너 최초 기동 시 자동 실행
# - 이미 실행 중인 컨테이너에서는 scripts/load_taxos.sh 로 다시 적재 (기존 taxos DB는 지우고 새로 만듦)
set -euo pipefail

DATA=/datasets/taxos
U="$POSTGRES_USER"

psql -v ON_ERROR_STOP=1 -q -U "$U" -d postgres -c "DROP DATABASE IF EXISTS taxos WITH (FORCE);"
psql -v ON_ERROR_STOP=1 -q -U "$U" -d postgres -c "CREATE DATABASE taxos OWNER \"$U\";"
psql -v ON_ERROR_STOP=1 -q -U "$U" -d taxos -f "$DATA/schema.sql"

# 외래키 순서대로 적재
for t in accountants customers tax_returns document_types document_submissions consultations tasks risk_events; do
  psql -v ON_ERROR_STOP=1 -q -U "$U" -d taxos \
    -c "\copy $t FROM '$DATA/data/$t.csv' WITH (FORMAT csv, HEADER true)"
done
# 장이 진행되며 추가된 테이블 (예: 23장 notifications) — 이름순으로 적용
for m in "$DATA"/migrations/*.sql; do
  [ -e "$m" ] && psql -v ON_ERROR_STOP=1 -q -U "$U" -d taxos -f "$m"
done
echo "[initdb] taxos loaded"
