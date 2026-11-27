-- 30장: OWL 추론 결과(고객 분류)를 운영형으로 받아 두는 테이블.
-- 시스템 실행자(추론 동기화 작업)의 Action 으로만 바뀐다. 해제해도 지우지 않는다.
CREATE TABLE IF NOT EXISTS customer_segments (
    segment_id  varchar(12) PRIMARY KEY,
    customer_id varchar(10) NOT NULL REFERENCES customers(customer_id),
    segment     varchar(20) NOT NULL CHECK (segment IN ('기한후신고이력', '연락두절이력', '반복위험')),
    status      varchar(10) NOT NULL CHECK (status IN ('유효', '해제')),
    inferred_by varchar(60) NOT NULL,                 -- 추론에 쓴 모델과 버전
    evidence    text,                                 -- 근거가 된 객체 ID
    assigned_at timestamp   NOT NULL,
    released_at timestamp,
    released_by varchar(20),                          -- 해제한 실행자 (사람이 해제하면 다음 동기화가 존중한다)
    CHECK ((status = '해제') = (released_at IS NOT NULL))
);
-- 같은 고객·분류의 '유효' 기록은 하나만
CREATE UNIQUE INDEX IF NOT EXISTS customer_segments_active ON customer_segments (customer_id, segment)
    WHERE status = '유효';
