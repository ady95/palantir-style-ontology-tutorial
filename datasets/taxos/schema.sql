-- AI Native Tax OS 실습용 합성 데이터 스키마
-- 모든 데이터는 scripts/generate_taxos_data.py 로 생성한 가상 데이터입니다.

SET client_min_messages = warning;

-- 담당자(세무사·실무자)
CREATE TABLE accountants (
    accountant_id   varchar(10) PRIMARY KEY,
    name            varchar(30) NOT NULL,
    role            varchar(10) NOT NULL CHECK (role IN ('세무사', '실무자')),
    team            varchar(20) NOT NULL,
    max_workload    smallint    NOT NULL CHECK (max_workload > 0),
    can_approve     boolean     NOT NULL DEFAULT false,
    email           varchar(60) NOT NULL
);

-- 고객(사업자)
CREATE TABLE customers (
    customer_id       varchar(10) PRIMARY KEY,
    company_name      varchar(40) NOT NULL,
    representative    varchar(30) NOT NULL,
    business_reg_no   varchar(12) NOT NULL UNIQUE,   -- 999-로 시작하는 가상 번호
    business_type     varchar(10) NOT NULL CHECK (business_type IN ('개인', '법인')),
    vat_type          varchar(10) NOT NULL CHECK (vat_type IN ('일반과세자', '간이과세자')),
    industry          varchar(30) NOT NULL,
    status            varchar(10) NOT NULL CHECK (status IN ('영업', '휴업', '폐업')),
    risk_level        varchar(10) NOT NULL CHECK (risk_level IN ('낮음', '보통', '높음')),
    accountant_id     varchar(10) NOT NULL REFERENCES accountants(accountant_id),
    phone             varchar(20) NOT NULL,
    contracted_at     date        NOT NULL,
    CHECK (NOT (business_type = '법인' AND vat_type = '간이과세자'))
);

-- 세무 신고
CREATE TABLE tax_returns (
    return_id      varchar(12) PRIMARY KEY,
    customer_id    varchar(10) NOT NULL REFERENCES customers(customer_id),
    tax_kind       varchar(20) NOT NULL,           -- 예: 부가가치세 확정신고
    period         varchar(20) NOT NULL,           -- 예: 2026-2기
    due_date       date        NOT NULL,
    status         varchar(10) NOT NULL CHECK (status IN ('자료수집중', '검토중', '신고완료', '기한후신고')),
    filed_at       date,
    CHECK ((status IN ('신고완료', '기한후신고')) = (filed_at IS NOT NULL)),
    UNIQUE (customer_id, tax_kind, period)
);

-- 필수 자료 유형 (사업자 유형별 적용 여부)
CREATE TABLE document_types (
    doc_type_code     varchar(5)  PRIMARY KEY,
    doc_name          varchar(40) NOT NULL,
    for_general       boolean     NOT NULL,   -- 개인 일반과세자
    for_simplified    boolean     NOT NULL,   -- 개인 간이과세자
    for_corporation   boolean     NOT NULL    -- 법인
);

-- 신고별 자료 제출 현황 (명시적 상태로 관리)
CREATE TABLE document_submissions (
    submission_id   varchar(14) PRIMARY KEY,
    return_id       varchar(12) NOT NULL REFERENCES tax_returns(return_id),
    doc_type_code   varchar(5)  NOT NULL REFERENCES document_types(doc_type_code),
    status          varchar(10) NOT NULL CHECK (status IN ('미요청', '요청됨', '제출완료', '해당없음')),
    requested_at    date,
    submitted_at    date,
    CHECK (status <> '제출완료' OR submitted_at IS NOT NULL),
    CHECK (status <> '요청됨' OR requested_at IS NOT NULL),
    UNIQUE (return_id, doc_type_code)
);

-- 상담 이력
CREATE TABLE consultations (
    consultation_id  varchar(12) PRIMARY KEY,
    customer_id      varchar(10) NOT NULL REFERENCES customers(customer_id),
    accountant_id    varchar(10) NOT NULL REFERENCES accountants(accountant_id),
    channel          varchar(10) NOT NULL CHECK (channel IN ('전화', '카카오톡', '이메일', '방문')),
    consulted_at     timestamp   NOT NULL,
    topic            varchar(30) NOT NULL,
    summary          text        NOT NULL
);

-- 후속 업무
CREATE TABLE tasks (
    task_id        varchar(12) PRIMARY KEY,
    customer_id    varchar(10) NOT NULL REFERENCES customers(customer_id),
    return_id      varchar(12) REFERENCES tax_returns(return_id),
    task_type      varchar(20) NOT NULL CHECK (task_type IN ('자료요청', '신고서검토', '고객연락', '담당자배정')),
    assignee_id    varchar(10) NOT NULL REFERENCES accountants(accountant_id),
    status         varchar(10) NOT NULL CHECK (status IN ('대기', '진행중', '완료', '취소')),
    due_date       date        NOT NULL,
    created_by     varchar(10) NOT NULL CHECK (created_by IN ('human', 'agent', 'system')),
    created_at     timestamp   NOT NULL,
    note           text
);

-- 위험 이벤트
CREATE TABLE risk_events (
    event_id      varchar(12) PRIMARY KEY,
    customer_id   varchar(10) NOT NULL REFERENCES customers(customer_id),
    event_type    varchar(20) NOT NULL CHECK (event_type IN ('기한후신고', '가산세부과', '연락두절', '자료지연제출')),
    occurred_at   date        NOT NULL,
    note          text
);

CREATE INDEX ON customers (accountant_id);
CREATE INDEX ON tax_returns (customer_id);
CREATE INDEX ON tax_returns (due_date, status);
CREATE INDEX ON document_submissions (return_id);
CREATE INDEX ON consultations (customer_id);
CREATE INDEX ON tasks (customer_id);
CREATE INDEX ON tasks (assignee_id, status);
CREATE INDEX ON risk_events (customer_id);
