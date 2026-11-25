# Northwind (PostgreSQL)

공개 샘플 관계형 DB입니다. 실제 기업 운영 데이터가 아닙니다.

- 출처: https://github.com/pthom/northwind_psql (`northwind.sql`)
- 원본: Microsoft Northwind 샘플 데이터베이스
- 라이선스: Microsoft Public License (Ms-PL) — 같은 폴더의 `LICENSE` 참고

`docker compose up -d`를 처음 실행하면 `northwind` 데이터베이스에 자동으로 적재됩니다
(`docker/postgres/initdb/01-northwind.sh`).

| 테이블 | 온톨로지 Object 후보 |
|---|---|
| customers | Customer |
| orders | Order |
| order_details | Order–Product Link (수량·단가·할인 속성) |
| products | Product |
| suppliers | Supplier |
| employees | Employee |
| categories | Category |
| shippers | Shipper |

발주 승인·업무 상태·감사 로그용 테이블은 이후 장에서 별도로 추가합니다.
