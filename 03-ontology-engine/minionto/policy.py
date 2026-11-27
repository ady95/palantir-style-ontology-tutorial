"""Policy Engine — 행·속성 단위 권한 정책과 판단 기록 (21장)

정의 파일의 security 섹션:

    security:
      row_policies:                    # 행: 이 실행자에게 보이는 객체
        own_customers:
          description: 실무자는 본인 담당 고객과 그 고객의 신고·자료·업무만 다룬다
          roles: [실무자]
          rules:
            Customer:  { field: accountant_id, op: eq, value: $actor.id }
            TaxReturn: { field: customer.accountant_id, op: eq, value: $actor.id }
      property_policies:               # 속성: 이 실행자에게 감출 속성
        personal_info:
          description: AI 에이전트는 고객의 개인 식별 정보를 보지 않는다
          actor_types: [agent]
          hidden:
            Customer: [representative, phone, business_reg_no]
      audit_reads: [agent]             # 이 유형의 실행자가 조회하면 기록을 남긴다

정책을 집행하는 곳(Policy Enforcement Point)
- ObjectSet.secure(actor): 행 조건을 덧붙이고, 감춘 속성은 결과에서 빼며 조건·정렬·선택에 쓰면 거부
- Action Engine: 대상이 실행자에게 보이지 않으면 denied (조회할 수 없는 것은 바꿀 수도 없다)
- Function: 입력 객체가 실행자에게 보여야 하고, 출력 객체의 감춘 속성은 뺀다 (계산 자체는 전체 데이터로)
엔진 내부의 조건 평가·변경은 정책과 무관하게 전체 데이터를 본다 (as_actor 는 $actor 값만 정한다).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .metamodel import Condition, PolicyTarget
from .query import QueryError
from .schema import PathError

if TYPE_CHECKING:
    from .objects import OntologyClient, OntoObject


class PolicyError(QueryError):
    """정책이 막은 요청 (API 에서는 403)"""


class PolicyEngine:
    def __init__(self, client: OntologyClient):
        self.client = client
        self.security = client.onto.security

    # ------------------------------------------------------------------ 어떤 정책이 적용되나
    @staticmethod
    def applies(pol: PolicyTarget, actor: dict | None) -> bool:
        if not actor:
            return False
        if pol.roles and actor.get("role") not in pol.roles:
            return False
        if pol.actor_types and actor.get("type") not in pol.actor_types:
            return False
        return True

    def row_rules(self, object_type: str, actor: dict | None) -> list[tuple[str, Condition]]:
        """(정책 이름, 보이는 조건) — 여럿이면 모두 만족해야 보인다"""
        return [(name, pol.rules[object_type]) for name, pol in self.security.row_policies.items()
                if object_type in pol.rules and self.applies(pol, actor)]

    def hidden(self, object_type: str, actor: dict | None) -> dict[str, str]:
        """감출 속성 → 정책 이름"""
        out: dict[str, str] = {}
        for name, pol in self.security.property_policies.items():
            if self.applies(pol, actor):
                for prop in pol.hidden.get(object_type, []):
                    out.setdefault(prop, name)
        return out

    def describe_for(self, actor: dict) -> dict:
        """이 실행자에게 적용되는 정책 요약 (API 메타데이터·AI 에이전트용)"""
        rows = {n: p.description for n, p in self.security.row_policies.items() if self.applies(p, actor)}
        props = {n: p.description for n, p in self.security.property_policies.items() if self.applies(p, actor)}
        hidden = {t: sorted(self.hidden(t, actor)) for t in self.client.onto.object_types if self.hidden(t, actor)}
        return {"row_policies": rows, "property_policies": props, "hidden_properties": hidden}

    # ------------------------------------------------------------------ 속성 검사
    def check_path(self, object_type: str, path: str, actor: dict, use: str) -> None:
        """경로의 마지막 속성이 실행자에게 감춰져 있으면 거부 (링크를 거친 경로 포함)"""
        try:
            res = self.client.schema.resolve(object_type, path)
        except PathError:
            return                                      # 경로 오류는 조회 엔진이 따로 보고한다
        if res.prop is None:
            return
        prop = path.split(".")[-1]
        pol = self.hidden(res.object_type, actor).get(prop)
        if pol:
            self.log(actor, "deny", "property", res.object_type, policy=pol,
                     detail={"property": prop, "use": use, "path": path, "from": object_type})
            raise PolicyError(f"정책 '{pol}': {res.object_type}.{prop} 은 볼 수 없는 속성이라 {use}에 쓸 수 없습니다")

    def check_condition(self, object_type: str, cond: Condition, actor: dict) -> None:
        if cond.field is not None and not cond.field.startswith("$"):
            self.check_path(object_type, cond.field, actor, "조건")
        for sub in (cond.all_ or []) + (cond.any_ or []) + ([cond.not_] if cond.not_ else []):
            self.check_condition(object_type, sub, actor)
        for q in (cond.some, cond.none):
            if q is not None and q.where is not None:
                end = self.client.schema.links[object_type].get(q.link)
                if end is not None:
                    self.check_condition(end.target, q.where, actor)

    # ------------------------------------------------------------------ 객체 단위
    def visible(self, object_type: str, pk: Any, actor: dict) -> bool:
        if not self.row_rules(object_type, actor):
            return True
        ot = self.client.onto.object_types[object_type]
        return self.client.objects(object_type).where(**{ot.source.primary_key: pk}).secure(actor).exists()

    def require_visible(self, object_type: str, pk: Any, actor: dict, use: str) -> None:
        """보이지 않으면 판단을 기록하고 PolicyError (메시지에 정책 설명)"""
        if self.visible(object_type, pk, actor):
            return
        names = [n for n, _ in self.row_rules(object_type, actor)]
        self.log(actor, "deny", "row", object_type, str(pk), policy=", ".join(names), detail={"use": use})
        desc = "; ".join(self.security.row_policies[n].description for n in names)
        raise PolicyError(f"정책 '{', '.join(names)}': {desc} ({object_type} {pk} 은 범위 밖)")

    def redact(self, value: Any, actor: dict) -> Any:
        """Function 출력 등에 섞인 객체에서 감춘 속성을 뺀다"""
        from .objects import OntoObject
        if isinstance(value, OntoObject):
            hidden = self.hidden(value.object_type, actor)
            if not hidden:
                return value
            props = {k: v for k, v in value.properties.items() if k not in hidden}
            return OntoObject(self.client, value.object_type, props, restricted=frozenset(hidden), viewer=actor)
        if isinstance(value, dict):
            return {k: self.redact(v, actor) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v, actor) for v in value]
        return value

    # ------------------------------------------------------------------ 판단 기록
    def log(self, actor: dict, decision: str, kind: str, object_type: str | None = None,
            target_pk: str | None = None, *, policy: str | None = None, detail: dict | None = None) -> None:
        from .actions import ENGINE_SCHEMA, _json
        self.client.actions.ensure_tables()
        self.client.conn.execute(
            f"""INSERT INTO {ENGINE_SCHEMA}.policy_decisions
                (actor, decision, kind, object_type, target_pk, policy, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (_json(actor), decision, kind, object_type, target_pk, policy, _json(detail or {})))

    def log_read(self, actor: dict, object_type: str, rows: int, restricted: list[str], how: str) -> None:
        if actor.get("type") not in self.security.audit_reads:
            return
        self.log(actor, "allow", "read", object_type,
                 detail={"rows": rows, "how": how, "hidden": sorted(restricted),
                         "row_policies": [n for n, _ in self.row_rules(object_type, actor)]})

    def decisions(self, limit: int = 50) -> list[dict]:
        from .actions import ENGINE_SCHEMA
        self.client.actions.ensure_tables()
        return self.client.conn.execute(
            f"""SELECT created_at, actor->>'id' AS actor_id, actor->>'type' AS actor_type, decision, kind,
                       object_type, target_pk, policy, detail
                FROM {ENGINE_SCHEMA}.policy_decisions ORDER BY decision_id DESC LIMIT %s""", (limit,)).fetchall()
