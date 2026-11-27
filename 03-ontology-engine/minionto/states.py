"""상태 머신 — 객체 타입의 상태와 Action 이 일으키는 전이 (20장)

정의 예 (ontology.yaml 의 객체 타입 아래):

    state_machine:
      property: status
      initial: [자료수집중]
      terminal: [신고완료, 기한후신고]
      transitions:
        start_review:    { from: 자료수집중, to: 검토중 }
        complete_filing: { from: 검토중,     to: 신고완료 }

엔진은 이 선언으로 세 가지를 한다 (actions.py).
- 실행 전: 대상의 현재 상태가 from 에 없으면 rejected (실행 조건과 함께 사유를 돌려준다)
- 변경 시: 상태 속성을 바꾸는 모든 변경(대상·링크 객체·새 객체)이 선언된 전이인지 다시 확인
- 변경 후: 일어난 전이를 minionto.state_transitions 에 기록
선언과 Action 의 edits 가 서로 맞는지는 validator.py 가 정의 단계에서 검사한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metamodel import Ontology, StateMachine


@dataclass(frozen=True)
class TransitionRule:
    object_type: str
    property: str
    action: str
    from_: tuple[str, ...]
    to: str

    def __str__(self) -> str:
        return f"{self.object_type}.{self.property}: {' | '.join(self.from_)} → {self.to} ({self.action})"


class StateMachines:
    def __init__(self, onto: Ontology):
        self.onto = onto

    def machine(self, object_type: str) -> StateMachine | None:
        ot = self.onto.object_types.get(object_type)
        return ot.state_machine if ot else None

    def rule(self, object_type: str, action: str) -> TransitionRule | None:
        """action 이 object_type 의 상태를 바꾸는 규칙 (없으면 None)"""
        m = self.machine(object_type)
        t = m.transitions.get(action) if m else None
        return TransitionRule(object_type, m.property, action, tuple(t.from_), t.to) if t else None

    def rules_for_action(self, action: str) -> list[TransitionRule]:
        """action 이 일으키는 모든 전이 (대상과 링크로 이어진 객체 포함)"""
        return [r for name in self.onto.object_types if (r := self.rule(name, action))]

    def next_actions(self, object_type: str, state: str) -> dict[str, str]:
        """현재 상태에서 선언상 가능한 Action → 다음 상태"""
        m = self.machine(object_type)
        if m is None:
            return {}
        return {a: t.to for a, t in m.transitions.items() if state in t.from_}

    def mermaid(self, object_type: str) -> str:
        """Mermaid 상태 다이어그램 (상태 이름은 한글일 수 있어 s0, s1 … 별칭을 쓴다)"""
        m = self.machine(object_type)
        if m is None:
            raise ValueError(f"'{object_type}' 에 상태 머신이 없습니다")
        states = self.onto.object_types[object_type].properties[m.property].values or []
        sid = {s: f"s{i}" for i, s in enumerate(states)}
        lines = ["stateDiagram-v2"] + [f'    state "{s}" as {sid[s]}' for s in states]
        lines += [f"    [*] --> {sid[s]}" for s in m.initial]
        for action, t in m.transitions.items():
            lines += [f"    {sid[f]} --> {sid[t.to]}: {action}" for f in t.from_]
        lines += [f"    {sid[s]} --> [*]" for s in m.terminal]
        return "\n".join(lines)
