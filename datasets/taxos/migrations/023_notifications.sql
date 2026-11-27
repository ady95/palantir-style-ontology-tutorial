-- 23장: 외부 발송 결과를 온톨로지 객체(Notification)로 남기는 테이블
-- 기본 스키마(schema.sql) 적재 뒤 02-taxos.sh 가 migrations/*.sql 을 이름순으로 적용한다.
CREATE TABLE IF NOT EXISTS notifications (
    notification_id varchar(12) PRIMARY KEY,
    event_id        bigint      NOT NULL,                 -- minionto.outbox.event_id (다시 보내면 결과가 하나 더 생긴다)
    event_type      varchar(40) NOT NULL,
    customer_id     varchar(10) NOT NULL REFERENCES customers(customer_id),
    return_id       varchar(12) REFERENCES tax_returns(return_id),
    channel         varchar(10) NOT NULL CHECK (channel IN ('문자', '메일', '메신저')),
    recipient       varchar(10) NOT NULL CHECK (recipient IN ('고객', '세무사')),
    status          varchar(10) NOT NULL CHECK (status IN ('발송완료', '발송실패')),
    external_id     varchar(60),                          -- 외부 발송 서비스의 메시지 ID
    attempts        integer     NOT NULL CHECK (attempts >= 1),
    sent_at         timestamp   NOT NULL,
    detail          text
);
CREATE INDEX IF NOT EXISTS notifications_return ON notifications (return_id);
CREATE INDEX IF NOT EXISTS notifications_event ON notifications (event_id);
