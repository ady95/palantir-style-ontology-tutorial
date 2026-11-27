-- 33장: 고객 문의 (상담 응대 에이전트가 34장에서 다룬다)
CREATE TABLE IF NOT EXISTS inquiries (
    inquiry_id   varchar(12) PRIMARY KEY,
    customer_id  varchar(10) NOT NULL REFERENCES customers(customer_id),
    channel      varchar(10) NOT NULL CHECK (channel IN ('카카오톡', '이메일', '전화', '홈페이지')),
    received_at  timestamp   NOT NULL,
    question     text        NOT NULL,
    category     varchar(10) NOT NULL CHECK (category IN ('자료제출', '신고일정', '세금계산', '휴폐업', '기타')),
    status       varchar(10) NOT NULL CHECK (status IN ('접수', '답변완료', '담당자확인')),
    answer       text,
    answered_by  varchar(20),
    answered_at  timestamp,
    escalation   text,
    CHECK ((status = '답변완료') = (answer IS NOT NULL))
);
