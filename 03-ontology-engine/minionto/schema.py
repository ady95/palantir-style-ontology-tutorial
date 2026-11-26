"""온톨로지 탐색 도우미 — 객체 타입별 '링크 이름 → 대상' 색인과 경로 해석

validator 와 이후의 Object/Link Engine 이 함께 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metamodel import LinkType, ObjectType, Ontology, Property

# (링크 방향별) 출발 쪽에서 본 상대 객체 수: True = 여러 개
_FORWARD_MANY = {"one_to_one": False, "one_to_many": True, "many_to_one": False, "many_to_many": True}
_REVERSE_MANY = {"one_to_one": False, "one_to_many": False, "many_to_one": True, "many_to_many": True}


@dataclass(frozen=True)
class LinkEnd:
    """어떤 객체 타입에서 링크 이름으로 이동했을 때의 정보"""
    name: str           # 이 객체 타입에서 부르는 이름 (forward_name 또는 reverse_name)
    link_type: str      # link_types 의 키
    target: str         # 도착 객체 타입
    many: bool          # 도착 객체가 여러 개인가
    forward: bool       # 링크 정의의 from → to 방향인가


@dataclass
class Resolved:
    object_type: str            # 경로 마지막 단계가 속한 객체 타입
    prop: Property | None       # 마지막이 속성이면 그 정의
    link: LinkEnd | None        # 마지막이 링크면 그 정의
    many: bool                  # 경로 중간에 '여러 개' 링크를 거쳤는가


class PathError(ValueError):
    pass


class Schema:
    def __init__(self, onto: Ontology):
        self.onto = onto
        self.links: dict[str, dict[str, LinkEnd]] = {name: {} for name in onto.object_types}
        self.duplicates: list[tuple[str, str]] = []   # 같은 객체 타입에서 겹친 링크 이름
        for lname, lt in onto.link_types.items():
            self._add(lt.from_, LinkEnd(lt.forward_name, lname, lt.to, _FORWARD_MANY[lt.cardinality], True))
            self._add(lt.to, LinkEnd(lt.reverse_name, lname, lt.from_, _REVERSE_MANY[lt.cardinality], False))

    def _add(self, obj: str, end: LinkEnd) -> None:
        if obj not in self.links:
            return  # 존재하지 않는 객체 타입은 validator 가 따로 보고한다
        if end.name in self.links[obj]:
            self.duplicates.append((obj, end.name))
        self.links[obj][end.name] = end

    def object_type(self, name: str) -> ObjectType:
        return self.onto.object_types[name]

    def link_type(self, name: str) -> LinkType:
        return self.onto.link_types[name]

    def resolve(self, start: str, path: str) -> Resolved:
        """'customer.accountant.max_workload' 같은 경로를 start 객체 타입 기준으로 해석한다."""
        current, many = start, False
        parts = path.split(".")
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            ot = self.onto.object_types[current]
            if last and part in ot.properties:
                return Resolved(current, ot.properties[part], None, many)
            end = self.links[current].get(part)
            if end is None:
                kind = "속성 또는 링크" if last else "링크"
                raise PathError(f"'{current}' 에 '{part}' {kind}가 없습니다 (경로: {path})")
            if last:
                return Resolved(end.target, None, end, many or end.many)
            many = many or end.many
            current = end.target
        raise PathError(f"빈 경로입니다: '{path}'")
