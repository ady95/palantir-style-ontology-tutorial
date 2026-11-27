-- 설비 정비 합성 데이터 (35장: 세 번째 도메인으로 엔진 재사용성 확인)
-- 공장 두 라인의 설비·정비원·작업 지시. 실제 회사·인물과 무관한 합성 데이터이며, 기준일은 2027-01-18 이다.
-- 난수 없이 행 번호로 값을 정하므로 몇 번을 적재해도 같은 데이터가 나온다.

CREATE TABLE technicians (
    technician_id   varchar(4)  PRIMARY KEY,
    line            varchar(10) NOT NULL CHECK (line IN ('A라인', 'B라인')),
    position        varchar(10) NOT NULL CHECK (position IN ('정비팀장', '정비원')),
    skill           varchar(10) NOT NULL CHECK (skill IN ('전기', '기계')),
    phone           varchar(20),
    max_open_orders integer     NOT NULL CHECK (max_open_orders > 0)
);

CREATE TABLE equipment (
    equipment_id          varchar(5)  PRIMARY KEY,
    equipment_name        varchar(40) NOT NULL,
    line                  varchar(10) NOT NULL CHECK (line IN ('A라인', 'B라인')),
    criticality           varchar(2)  NOT NULL CHECK (criticality IN ('상', '중', '하')),
    status                varchar(10) NOT NULL CHECK (status IN ('가동', '정지', '폐기')),
    inspection_cycle_days integer     NOT NULL CHECK (inspection_cycle_days > 0),
    last_inspected        date        NOT NULL,
    technician_id         varchar(4)  REFERENCES technicians(technician_id)
);

CREATE TABLE work_orders (
    order_id       varchar(8)    PRIMARY KEY,
    equipment_id   varchar(5)    NOT NULL REFERENCES equipment(equipment_id),
    technician_id  varchar(4)    REFERENCES technicians(technician_id),
    kind           varchar(10)   NOT NULL CHECK (kind IN ('예방점검', '고장수리')),
    status         varchar(10)   NOT NULL CHECK (status IN ('접수', '배정', '작업중', '완료', '취소')),
    estimated_cost numeric(12,0) NOT NULL CHECK (estimated_cost >= 0),
    opened_at      date          NOT NULL,
    completed_at   date,
    created_by     varchar(10)   NOT NULL,
    note           text,
    CHECK ((status = '완료') = (completed_at IS NOT NULL)),
    CHECK (status = '접수' OR status = '취소' OR technician_id IS NOT NULL)
);
CREATE INDEX work_orders_equipment ON work_orders (equipment_id);
CREATE INDEX work_orders_technician ON work_orders (technician_id);

-- 정비원 8명: 라인마다 팀장 1명 + 정비원 3명
INSERT INTO technicians
SELECT 'E' || lpad(i::text, 2, '0'),
       CASE WHEN i <= 4 THEN 'A라인' ELSE 'B라인' END,
       CASE WHEN i IN (1, 5) THEN '정비팀장' ELSE '정비원' END,
       CASE WHEN i % 2 = 0 THEN '전기' ELSE '기계' END,
       '010-0000-' || lpad((1000 + i)::text, 4, '0'),
       4
FROM generate_series(1, 8) AS i;

-- 설비 40대: 홀수는 A라인, 짝수는 B라인. 담당은 그 라인의 정비원(팀장 제외) 3명이 돌아가며 맡는다
INSERT INTO equipment
SELECT 'M' || lpad(i::text, 3, '0'),
       (ARRAY['프레스', '컨베이어', '공압 실린더', '용접 로봇', '냉각기'])[(i % 5) + 1] || ' ' || lpad(i::text, 2, '0'),
       CASE WHEN i % 2 = 1 THEN 'A라인' ELSE 'B라인' END,
       (ARRAY['상', '중', '하'])[(i % 3) + 1],
       CASE WHEN i % 13 = 0 THEN '정지' WHEN i = 40 THEN '폐기' ELSE '가동' END,
       (ARRAY[30, 60, 90])[(i % 3) + 1],
       DATE '2027-01-18' - ((i * 11) % 120),
       'E' || lpad((CASE WHEN i % 2 = 1 THEN 2 ELSE 6 END + (i / 2) % 3)::text, 2, '0')
FROM generate_series(1, 40) AS i;

-- 작업 지시 60건: 1~36 완료, 37~40 취소, 41~46 작업중, 47~52 배정, 53~60 접수 (4건마다 1건이 고장수리)
INSERT INTO work_orders
SELECT 'W' || lpad(i::text, 5, '0'),
       e.equipment_id,
       CASE WHEN i BETWEEN 53 AND 60 OR i BETWEEN 37 AND 40 THEN NULL ELSE e.technician_id END,
       CASE WHEN i % 4 = 0 THEN '고장수리' ELSE '예방점검' END,
       CASE WHEN i <= 36 THEN '완료' WHEN i <= 40 THEN '취소' WHEN i <= 46 THEN '작업중'
            WHEN i <= 52 THEN '배정' ELSE '접수' END,
       CASE WHEN i % 4 = 0 THEN ((i % 5) + 1) * 1000000 ELSE 200000 END,
       DATE '2027-01-18' - (62 - i),
       CASE WHEN i <= 36 THEN DATE '2027-01-18' - (60 - i) END,
       CASE WHEN i % 3 = 0 THEN 'agent' ELSE 'human' END,
       NULL
FROM generate_series(1, 60) AS i
JOIN equipment e ON e.equipment_id = 'M' || lpad((((i * 7) % 39) + 1)::text, 3, '0');
