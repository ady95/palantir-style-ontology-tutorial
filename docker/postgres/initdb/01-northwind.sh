#!/bin/bash
# 컨테이너 최초 기동 시 1회 실행: northwind 데이터베이스 생성 및 데이터 적재
set -euo pipefail

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE DATABASE northwind OWNER \"$POSTGRES_USER\";"

psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d northwind -f /datasets/northwind/northwind.sql
echo "[initdb] northwind loaded"
