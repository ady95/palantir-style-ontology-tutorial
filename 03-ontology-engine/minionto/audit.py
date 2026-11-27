"""Audit Log — 흩어진 기록을 묶어 '누가, 언제, 무엇을, 왜'에 답한다 (24장)

엔진의 기록은 목적에 따라 여러 테이블에 있다.
    action_log         실행 (요청·판단·결과, 바꾼 값의 전후)        18장
    state_transitions  객체의 상태 전이                            20장
    policy_decisions   정책 판단 (거부, AI 의 조회)                 21장
    approval_requests  승인 요청과 결정                            18·21장
    outbox             이벤트와 발송 상태                          18·23장
    event_deliveries   구독자의 이벤트 처리                         24장
    (Notification 객체) 외부 발송 결과                              23장

    audit = AuditLog(client)
    audit.object_history("DocumentSubmission", "S000005")   # 이 객체에 일어난 모든 일
    audit.trace(execution_id)                               # 실행 하나에서 이어진 모든 일
    audit.verify()                                          # 기록이 빠지거나 고쳐지지 않았는가
    audit.metrics()                                         # 운영 지표
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .actions import ENGINE_SCHEMA as S

if TYPE_CHECKING:
    from .objects import OntologyClient

# 실행(action_log)이 아니라 엔진이 직접 만드는 이벤트 — execution_id 가 실행 기록을 가리키지 않는다
ENGINE_EVENTS = {"approval_decided", "approval_expired"}


@dataclass
class Entry:
    at: Any
    source: str             # action | transition | approval | event | policy
    summary: str
    ref: str = ""           # execution_id · approval_id · event_id
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{str(self.at)[:19]}  {self.source:10s} {self.summary}"


@dataclass
class Finding:
    check: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.message}"


class AuditLog:
    def __init__(self, client: OntologyClient):
        self.client = client
        client.actions.ensure_tables()

    def _q(self, sql: str, *args) -> list[dict]:
        return self.client.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ 객체별 이력
    def object_history(self, object_type: str, pk: Any) -> list[Entry]:
        """이 객체를 대상으로 한 실행, 이 객체를 바꾼 실행(다른 객체가 대상이어도), 상태 전이, 승인, 이벤트, 정책 판단"""
        pk = str(pk)
        out: list[Entry] = []
        probe = json.dumps([{"object_type": object_type, "pk": pk}], ensure_ascii=False)
        for r in self._q(f"""SELECT * FROM {S}.action_log
                             WHERE (object_type = %s AND target_pk = %s) OR changes @> %s::jsonb
                             ORDER BY seq""", object_type, pk, probe):
            mine = [c for c in r["changes"] if c["object_type"] == object_type and str(c["pk"]) == pk]
            who = f"{r['actor'].get('id')}({r['actor'].get('type')})"
            what = "; ".join(_diff(c) for c in mine) if mine else (r["messages"][0] if r["messages"] else "")
            via = "" if (r["object_type"], r["target_pk"]) == (object_type, pk) else \
                f" ← {r['object_type']}({r['target_pk']}) 대상"
            out.append(Entry(r["created_at"], "action", f"{r['action']} [{r['status']}] {who}{via}: {what}",
                             str(r["execution_id"])))
        for r in self._q(f"""SELECT * FROM {S}.approval_requests WHERE object_type = %s AND target_pk = %s""",
                         object_type, pk):
            out.append(Entry(r["created_at"], "approval",
                             f"{r['action']} 승인 요청 {r['requested_by'].get('id')} → {r['status']}"
                             + (f" ({r['decided_by'].get('id')})" if r["decided_by"] else ""), str(r["approval_id"])))
        for r in self._q(f"""SELECT o.*, coalesce(l.created_at, o.created_at) AS at FROM {S}.outbox o
                             LEFT JOIN {S}.action_log l ON l.execution_id = o.execution_id
                             WHERE o.object_type = %s AND o.target_pk = %s""", object_type, pk):
            out.append(Entry(r["at"], "event",                     # 이벤트는 그것을 낸 실행의 시각으로
                             f"#{r['event_id']} {r['event_type']} [{r['status']}, 시도 {r['attempts']}]",
                             str(r["event_id"])))
        for r in self._q(f"""SELECT * FROM {S}.policy_decisions WHERE object_type = %s AND target_pk = %s""",
                         object_type, pk):
            out.append(Entry(r["created_at"], "policy",
                             f"{r['decision']} {r['kind']} {r['actor'].get('id')} ({r['policy']})"))
        return sorted(out, key=lambda e: (e.at, e.source == "event"))

    # ------------------------------------------------------------------ 실행 하나의 추적
    def trace(self, execution_id: str) -> dict:
        """실행 하나에서 이어진 기록: 실행자(토큰) → 실행 → 상태 전이 → 이벤트 → 발송·구독 → 후속 실행"""
        run = self._q(f"SELECT * FROM {S}.action_log WHERE execution_id = %s", execution_id)
        if not run:
            return {}
        run = run[0]
        out = {"execution": {k: run[k] for k in ("action", "object_type", "target_pk", "actor", "status",
                                                 "messages", "approval_id", "created_at")},
               "changes": run["changes"],
               "transitions": self._q(f"""SELECT object_type, target_pk, from_state, to_state
                                          FROM {S}.state_transitions WHERE execution_id = %s
                                          ORDER BY transition_id""", execution_id),
               "approval": None, "events": []}
        if run["approval_id"]:
            out["approval"] = self._q(f"""SELECT status, requested_by->>'id' AS requested_by,
                                              decided_by->>'id' AS decided_by, comment
                                          FROM {S}.approval_requests WHERE approval_id = %s""", run["approval_id"])
        notif = "notifications" if self._has_table("notifications") else None
        for ev in self._q(f"""SELECT event_id, event_type, status, attempts, published_at FROM {S}.outbox
                              WHERE execution_id = %s ORDER BY event_id""", execution_id):
            ev["deliveries"] = self._q(f"""SELECT subscriber, status, result, execution_id, detail
                                          FROM {S}.event_deliveries WHERE event_id = %s""", ev["event_id"])
            ev["notifications"] = self._q(
                f"""SELECT notification_id, status, external_id, attempts FROM {notif} WHERE event_id = %s
                    ORDER BY notification_id""", ev["event_id"]) if notif else []
            out["events"].append(ev)
        return out

    def _has_table(self, name: str) -> bool:
        return self._q("SELECT to_regclass(%s) IS NOT NULL AS ok", name)[0]["ok"]

    # ------------------------------------------------------------------ 완전성 점검
    def verify(self) -> list[Finding]:
        found: list[Finding] = []
        # 1) 해시 체인: 기록을 고치면 다시 계산한 해시가, 지우면 다음 기록의 prev_hash 가 맞지 않는다
        rows = self._q(f"""SELECT seq, execution_id, action, prev_hash, row_hash,
                                  {S}.log_digest(prev_hash, l) AS recomputed,
                                  lag(row_hash) OVER (ORDER BY seq) AS actual_prev
                           FROM {S}.action_log l WHERE row_hash IS NOT NULL ORDER BY seq""")
        for i, r in enumerate(rows):
            if r["recomputed"] != r["row_hash"]:
                found.append(Finding("chain", f"seq {r['seq']} {r['action']}: 기록 내용이 해시와 다릅니다 (변조)"))
            expected_prev = r["actual_prev"] if i else None          # 체인의 첫 기록은 앞 해시가 없다
            if r["prev_hash"] != expected_prev:
                found.append(Finding("chain", f"seq {r['seq']} {r['action']}: 앞 기록과 이어지지 않습니다 "
                                              "(앞 기록이 삭제되었거나 순서가 바뀜)"))
        # 2) 상태 전이 기록 누락: 완료된 실행이 상태 속성을 바꿨는데 전이 기록이 없다
        machines = {n: ot.state_machine.property for n, ot in self.client.onto.object_types.items()
                    if ot.state_machine}
        for r in self._q(f"""SELECT l.execution_id, l.action, l.changes,
                                    (SELECT count(*) FROM {S}.state_transitions t
                                     WHERE t.execution_id = l.execution_id) AS n
                             FROM {S}.action_log l WHERE l.status = 'completed'"""):
            need = sum(1 for c in r["changes"] if machines.get(c["object_type"]) in c["after"])
            if need > r["n"]:
                found.append(Finding("transitions", f"{r['action']} {str(r['execution_id'])[:8]}: 상태 변경 "
                                                    f"{need}건 중 전이 기록 {r['n']}건"))
        # 3) 이벤트의 출처: 실행에서 나온 이벤트는 실행 기록이 있어야 한다
        for r in self._q(f"""SELECT o.event_id, o.event_type FROM {S}.outbox o
                             WHERE NOT (o.event_type = ANY(%s)) AND NOT EXISTS (
                                 SELECT 1 FROM {S}.action_log l WHERE l.execution_id = o.execution_id)""",
                         sorted(ENGINE_EVENTS)):
            found.append(Finding("events", f"#{r['event_id']} {r['event_type']}: 이벤트를 만든 실행 기록이 없습니다"))
        # 4) 엔진 밖 변경: 마지막으로 기록된 값(after)과 지금 DB 값이 다르다
        found += self._drift()
        return found

    def _drift(self) -> list[Finding]:
        last: dict[tuple[str, str], dict] = {}
        for r in self._q(f"SELECT changes FROM {S}.action_log WHERE status = 'completed' ORDER BY seq"):
            for c in r["changes"]:
                last.setdefault((c["object_type"], str(c["pk"])), {}).update(c["after"])
        found = []
        for (otype, pk), after in last.items():
            ot = self.client.onto.object_types.get(otype)
            if ot is None:
                continue
            obj = self.client.get(otype, pk)
            if obj is None:
                found.append(Finding("drift", f"{otype}({pk}): 기록에는 있는데 지금은 없습니다"))
                continue
            for prop, value in after.items():
                p = ot.properties.get(prop)
                if p is None or p.type == "datetime" or prop == ot.source.primary_key:
                    continue
                now = obj.properties.get(prop)
                if _norm(now) != _norm(value):
                    found.append(Finding("drift", f"{otype}({pk}).{prop}: 마지막 기록 '{value}', 지금 '{now}' "
                                                  "— 엔진 밖에서 바뀌었습니다"))
        return found

    # ------------------------------------------------------------------ 운영 지표
    def metrics(self) -> dict:
        m: dict[str, Any] = {}
        m["actions"] = self._q(f"""SELECT actor->>'type' AS actor_type, status, count(*) AS n FROM {S}.action_log
                                   GROUP BY 1, 2 ORDER BY 1, 2""")
        m["approvals"] = self._q(f"""SELECT status, count(*) AS n,
                                        round(avg(extract(epoch FROM decided_at - created_at))::numeric, 1) AS avg_sec
                                     FROM {S}.approval_requests GROUP BY 1 ORDER BY 1""")
        m["outbox"] = self._q(f"SELECT status, count(*) AS n FROM {S}.outbox GROUP BY 1 ORDER BY 1")
        m["deliveries"] = self._q(f"""SELECT subscriber, status, result, count(*) AS n FROM {S}.event_deliveries
                                      GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
        m["policy"] = self._q(f"""SELECT decision, kind, count(*) AS n FROM {S}.policy_decisions
                                  GROUP BY 1, 2 ORDER BY 1, 2""")
        m["idempotency"] = self._q(f"""SELECT count(*) AS keys, coalesce(sum(replays), 0) AS replays
                                       FROM {S}.idempotency_keys""")[0]
        return m


def _diff(c: dict) -> str:
    before = c.get("before") or {}
    parts = [f"{k} {before.get(k)}→{v}" if c["op"] == "update" else f"{k}={v}"
             for k, v in c["after"].items() if k not in ("created_at",)]
    head = "생성" if c["op"] == "create" else "변경"
    return f"{head} " + ", ".join(parts[:4]) + (" …" if len(parts) > 4 else "")


def _norm(v: Any) -> str:
    return "" if v is None else str(v)
