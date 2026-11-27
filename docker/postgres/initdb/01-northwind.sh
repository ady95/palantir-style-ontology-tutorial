#!/bin/bash
# northwind 데이터베이스 생성 및 적재
# - 컨테이너 최초 기동 시 자동 실행
# - 이미 실행 중인 컨테이너에서는 scripts/load_northwind.sh 로 다시 적재 (기존 northwind DB는 지우고 새로 만듦)
set -euo pipefail

DATA=/datasets/northwind
U="$POSTGRES_USER"

psql -v ON_ERROR_STOP=1 -q -U "$U" -d postgres -c "DROP DATABASE IF EXISTS northwind WITH (FORCE);"
psql -v ON_ERROR_STOP=1 -q -U "$U" -d postgres -c "CREATE DATABASE northwind OWNER \"$U\";"
psql -v ON_ERROR_STOP=1 -q -U "$U" -d northwind -f "$DATA/northwind.sql"
# 실습용으로 추가한 테이블 (예: 32장 purchase_orders) — 이름순으로 적용
for m in "$DATA"/migrations/*.sql; do
  [ -e "$m" ] && psql -v ON_ERROR_STOP=1 -q -U "$U" -d northwind -f "$m"
done
echo "[initdb] northwind loaded"
