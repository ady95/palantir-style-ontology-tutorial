"""의미 모델(OWL) ↔ 운영형 정의(MiniOnto) 매핑 (29장)

매핑 파일(YAML)은 두 모델의 대응을 선언한다.
- 객체 타입 ↔ 클래스, 속성 값 ↔ 하위 클래스(subclasses.when), 속성 ↔ 데이터 속성, 링크 타입 ↔ 객체 속성
- 대응하지 않는 것은 이유와 함께 적는다 (not_mapped, semantic_only) — 빠뜨린 것과 일부러 뺀 것을 구분한다

할 수 있는 일
1. check(m)            매핑 검증: 양쪽에 있는 이름인가, 빠진 것은 없는가, 타입·도메인·카디널리티가 맞는가
2. axiom_checks(c, m)  OWL 공리(분리·포함)를 운영 데이터에 질의해 확인
3. export_rdf(c, m)    운영 데이터 → RDF (엔진을 거치므로 실행자의 권한 정책이 그대로 적용된다)
4. draft_from_owl(g)   OWL 에서 운영형 정의 초안 만들기 — 사람이 채울 곳(TODO)을 남긴다
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef
from rdflib.namespace import NamespaceManager

from .loader import load
from .metamodel import Ontology

ANNOTATIONS = {RDFS.label, RDFS.comment}
XSD_TYPES = {XSD.string: {"string", "enum"}, XSD.date: {"date"}, XSD.dateTime: {"datetime"},
             XSD.integer: {"integer"}, XSD.decimal: {"decimal"}, XSD.boolean: {"boolean"}}
OP_TYPE = {"string": XSD.string, "enum": XSD.string, "date": XSD.date, "datetime": XSD.dateTime,
           "integer": XSD.integer, "decimal": XSD.decimal, "float": XSD.decimal, "boolean": XSD.boolean}


# ---------------------------------------------------------------------- 매핑 읽기
@dataclass
class PropMap:
    name: str
    predicates: list[URIRef]
    as_: str | None = None


@dataclass
class TypeMap:
    object_type: str
    cls: URIRef
    iri: str
    subclasses: list[tuple[URIRef, dict]] = field(default_factory=list)
    props: list[PropMap] = field(default_factory=list)
    not_mapped: dict[str, str] = field(default_factory=dict)


@dataclass
class LinkMap:
    link_type: str
    prop: URIRef
    inverse: URIRef | None = None
    direction: str = "forward"


@dataclass
class Mapping:
    path: Path
    onto: Ontology
    owl: Graph
    base: str
    minimal_types: bool
    types: dict[str, TypeMap]
    links: dict[str, LinkMap]
    not_mapped_types: dict[str, str]
    not_mapped_links: dict[str, str]
    semantic_only: dict[URIRef, str]
    ns: NamespaceManager
    name: str = ""
    inferred: dict[URIRef, dict] = field(default_factory=dict)    # 정의 클래스 → 운영형으로 돌려받는 방법 (30장)
    sources: list[str] = field(default_factory=list)              # 의미 모델 파일 (본 파일 + 덧붙인 파일)

    @classmethod
    def load(cls, path: str | Path) -> Mapping:
        path = Path(path).resolve()
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        head = raw["mapping"]
        prefixes = head.get("prefixes", {})

        def iri(curie: str) -> URIRef:
            pre, _, local = curie.partition(":")
            if pre in prefixes:
                return URIRef(prefixes[pre] + local)
            return URIRef(curie)
        files = [head["semantic"], *head.get("semantic_extra", [])]
        owl = Graph()
        for f in files:
            owl.parse(path.parent / f)
        for p, ns in prefixes.items():
            owl.bind(p, ns)
        types = {}
        for ot, t in raw.get("object_types", {}).items():
            props = []
            for name, spec in (t.get("properties") or {}).items():
                if isinstance(spec, dict):
                    props.append(PropMap(name, [iri(spec["property"])], spec.get("as")))
                else:
                    props.append(PropMap(name, [iri(x) for x in (spec if isinstance(spec, list) else [spec])]))
            types[ot] = TypeMap(ot, iri(t["class"]), t["iri"],
                                [(iri(s["class"]), s["when"]) for s in t.get("subclasses", [])],
                                props, dict(t.get("not_mapped") or {}))
        links = {n: LinkMap(n, iri(x["property"]), iri(x["inverse"]) if x.get("inverse") else None,
                            x.get("direction", "forward")) for n, x in (raw.get("link_types") or {}).items()}
        nm = raw.get("not_mapped") or {}
        return cls(path, load(path.parent / head["operational"]), owl, prefixes[head.get("instance_base", "txd")],
                   head.get("minimal_types", True), types, links, dict(nm.get("object_types") or {}),
                   dict(nm.get("link_types") or {}), {iri(k): v for k, v in (raw.get("semantic_only") or {}).items()},
                   owl.namespace_manager, head.get("name", ""),
                   {iri(k): v for k, v in (raw.get("inferred") or {}).items()}, files)

    def short(self, u: URIRef) -> str:
        return u.n3(self.ns)

    # OWL 질의 도우미
    def supers(self, c: URIRef) -> set[URIRef]:
        """c 의 상위 클래스 전부 (자기 자신 제외, 이름 있는 것만)"""
        out, todo = set(), [c]
        while todo:
            for s in self.owl.objects(todo.pop(), RDFS.subClassOf):
                if isinstance(s, URIRef) and s not in out:
                    out.add(s)
                    todo.append(s)
        return out

    def is_a(self, c: URIRef, d: URIRef) -> bool:
        return c == d or d in self.supers(c)


# ---------------------------------------------------------------------- 1. 매핑 검증
@dataclass
class Finding:
    level: str          # error | warn | info
    side: str           # 운영형 | 의미 모델 | 타입 | 공리
    message: str


@dataclass
class Report:
    findings: list[Finding]
    coverage: dict[str, tuple[int, int, int]]     # 이름 → (매핑, 이유 있는 제외, 전체)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]


def check(m: Mapping) -> Report:
    f: list[Finding] = []
    onto, owl = m.onto, m.owl
    add = lambda lv, side, msg: f.append(Finding(lv, side, msg))  # noqa: E731
    classes = {c for c in owl.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)}
    dprops = set(owl.subjects(RDF.type, OWL.DatatypeProperty))
    oprops = set(owl.subjects(RDF.type, OWL.ObjectProperty))
    functional = set(owl.subjects(RDF.type, OWL.FunctionalProperty))
    inverse_of = {a: b for a, b in owl.subject_objects(OWL.inverseOf)}
    inverse_of |= {b: a for a, b in list(inverse_of.items())}

    # ---- 운영형 쪽: 모든 객체 타입·속성·링크가 매핑되거나 이유와 함께 빠졌는가
    cov = {"객체 타입": [0, 0, len(onto.object_types)], "속성": [0, 0, 0], "링크 타입": [0, 0, len(onto.link_types)]}
    for ot_name, ot in onto.object_types.items():
        t = m.types.get(ot_name)
        if t is None:
            if ot_name in m.not_mapped_types:
                cov["객체 타입"][1] += 1
            else:
                add("warn", "운영형", f"객체 타입 {ot_name} 이 매핑에 없고 이유도 없습니다")
            continue
        cov["객체 타입"][0] += 1
        cov["속성"][2] += len(ot.properties)
        mapped = {p.name for p in t.props}
        for name in mapped | set(t.not_mapped) | {k for _, w in t.subclasses for k in w}:
            if name not in ot.properties:
                add("error", "운영형", f"{ot_name}.{name} 은 운영형 정의에 없는 속성입니다")
        for name in ot.properties:
            if name in mapped:
                cov["속성"][0] += 1
            elif name in t.not_mapped:
                cov["속성"][1] += 1
            else:
                add("warn", "운영형", f"{ot_name}.{name} 이 매핑되지 않았고 이유도 없습니다")
        fields = re.findall(r"{(\w+)}", t.iri)
        if fields != [ot.source.primary_key]:
            add("error", "운영형", f"{ot_name} 의 IRI 템플릿은 기본키 {{{ot.source.primary_key}}} 하나만 써야 합니다")
        for c, when in t.subclasses:
            for k, v in when.items():
                p = ot.properties.get(k)
                if p is not None and p.values and v not in p.values:
                    add("error", "운영형", f"{ot_name}.{k} 에 '{v}' 값이 없습니다 (하위 클래스 {m.short(c)})")
    for lname in onto.link_types:
        if lname in m.links:
            cov["링크 타입"][0] += 1
        elif lname in m.not_mapped_links:
            cov["링크 타입"][1] += 1
        else:
            add("warn", "운영형", f"링크 타입 {lname} 이 매핑에 없고 이유도 없습니다")
    for lname in m.links:
        if lname not in onto.link_types:
            add("error", "운영형", f"링크 타입 {lname} 은 운영형 정의에 없습니다")

    # ---- 의미 모델 쪽: 쓴 이름이 OWL 에 있는가, OWL 의 클래스·속성이 모두 쓰였는가
    used_classes = {t.cls for t in m.types.values()} | {c for t in m.types.values() for c, _ in t.subclasses}
    used_props = {p for t in m.types.values() for pm in t.props for p in pm.predicates}
    used_props |= {lm.prop for lm in m.links.values()} | {lm.inverse for lm in m.links.values() if lm.inverse}
    for c in used_classes - classes:
        add("error", "의미 모델", f"{m.short(c)} 은 OWL 에 선언된 클래스가 아닙니다")
    for p in used_props - dprops - oprops - ANNOTATIONS:
        add("error", "의미 모델", f"{m.short(p)} 은 OWL 에 선언된 속성이 아닙니다")
    reasoned = set(m.semantic_only) | set(m.inferred)
    cov["클래스(OWL)"] = [len(classes & used_classes), len(classes & reasoned), len(classes)]
    for c in sorted(classes - used_classes - reasoned):
        add("warn", "의미 모델", f"클래스 {m.short(c)} 이 매핑에 없고 이유도 없습니다")
    allp = dprops | oprops
    cov["속성(OWL)"] = [len(allp & used_props), len(allp & set(m.semantic_only)), len(allp)]
    for p in sorted(allp - used_props - set(m.semantic_only)):
        add("warn", "의미 모델", f"속성 {m.short(p)} 이 매핑에 없고 이유도 없습니다")

    # ---- 타입: 값의 타입, 도메인, 치역, 함수형(카디널리티)
    for t in m.types.values():
        ot = onto.object_types.get(t.object_type)
        if ot is None:
            continue
        for pm in t.props:
            prop = ot.properties.get(pm.name)
            for pred in pm.predicates:
                if pred in ANNOTATIONS or prop is None:
                    continue
                rng = owl.value(pred, RDFS.range)
                ptype = "date" if pm.as_ == "date" else prop.type
                if rng in XSD_TYPES and ptype not in XSD_TYPES[rng]:
                    add("error", "타입", f"{t.object_type}.{pm.name}({prop.type}) ↔ {m.short(pred)}({m.short(rng)}) 타입이 다릅니다")
                dom = owl.value(pred, RDFS.domain)
                if dom is not None and not m.is_a(t.cls, dom):
                    add("error", "타입", f"{m.short(pred)} 의 도메인 {m.short(dom)} 에 {m.short(t.cls)} 가 속하지 않습니다")
                if pred in functional and prop.type == "list":
                    add("error", "타입", f"{m.short(pred)} 은 값이 하나(함수형)인데 {t.object_type}.{pm.name} 은 목록입니다")
    for lm in m.links.values():
        lt = onto.link_types.get(lm.link_type)
        if lt is None:
            continue
        subj, obj = (lt.from_, lt.to) if lm.direction == "forward" else (lt.to, lt.from_)
        card = lt.cardinality
        subj_one = card in (("many_to_one", "one_to_one") if lm.direction == "forward" else ("one_to_many", "one_to_one"))
        obj_one = card in (("one_to_many", "one_to_one") if lm.direction == "forward" else ("many_to_one", "one_to_one"))
        for side, ot_name, rel in (("도메인", subj, RDFS.domain), ("치역", obj, RDFS.range)):
            want = owl.value(lm.prop, rel)
            t = m.types.get(ot_name)
            if want is not None and t is not None and not m.is_a(t.cls, want):
                add("error", "타입", f"{lm.link_type}: {m.short(lm.prop)} 의 {side} {m.short(want)} 에 {ot_name} 가 맞지 않습니다")
        if lm.prop in functional and not subj_one:
            add("error", "타입", f"{lm.link_type}: {m.short(lm.prop)} 은 함수형인데 링크는 {subj} 하나에 여러 {obj} 입니다")
        inv = lm.inverse or inverse_of.get(lm.prop)
        if inv is not None and inv in functional and not obj_one:
            add("error", "타입", f"{lm.link_type}: 역관계 {m.short(inv)} 은 함수형인데 링크는 {obj} 하나에 여러 {subj} 입니다")
        if lm.inverse is not None and inverse_of.get(lm.prop) != lm.inverse:
            add("error", "의미 모델", f"{lm.link_type}: OWL 에 {m.short(lm.prop)} owl:inverseOf {m.short(lm.inverse)} 가 없습니다")
        if subj_one and lm.prop not in functional:
            add("info", "타입", f"{lm.link_type}: 운영형은 {subj} 하나에 {obj} 하나인데 OWL {m.short(lm.prop)} 은 함수형이 아닙니다 (OWL 쪽이 더 느슨함)")
    return Report(f, {k: tuple(v) for k, v in cov.items()})


# ---------------------------------------------------------------------- 2. 공리 → 데이터 질의
def axiom_checks(client, m: Mapping) -> list[dict]:
    """같은 객체 타입에 매핑된 하위 클래스 사이의 분리(disjointWith)·포함(subClassOf) 공리를 데이터로 확인"""
    out = []
    for t in m.types.values():
        subs = dict(t.subclasses)

        def cond(when: dict) -> dict:
            return {"all": [{"field": k, "op": "eq", "value": v} for k, v in when.items()]}
        for a, wa in subs.items():
            for b, wb in subs.items():
                if a == b:
                    continue
                disjoint = (a, OWL.disjointWith, b) in m.owl or (b, OWL.disjointWith, a) in m.owl
                if disjoint and str(a) < str(b):
                    n = client.objects(t.object_type).where({"all": [cond(wa), cond(wb)]}).count()
                    out.append({"axiom": f"{m.short(a)} owl:disjointWith {m.short(b)}", "object_type": t.object_type,
                                "query": f"{wa} 이면서 {wb}", "violations": n,
                                "structural": set(wa) == set(wb) and wa != wb})
                if (a, RDFS.subClassOf, b) in m.owl:
                    n = client.objects(t.object_type).where({"all": [cond(wa), {"not": cond(wb)}]}).count()
                    out.append({"axiom": f"{m.short(a)} rdfs:subClassOf {m.short(b)}", "object_type": t.object_type,
                                "query": f"{wa} 인데 {wb} 가 아님", "violations": n, "structural": False})
    return out


# ---------------------------------------------------------------------- 3. 운영 데이터 → RDF
def _literal(m: Mapping, pred: URIRef, value: Any, as_: str | None) -> Literal | None:
    if value is None or value == "":
        return None
    if as_ == "date" and hasattr(value, "date"):
        value = value.date()
    rng = m.owl.value(pred, RDFS.range)
    if rng in (XSD.date, XSD.integer, XSD.decimal, XSD.boolean, XSD.dateTime):
        return Literal(str(value), datatype=rng)
    return Literal(str(value))


def _types(m: Mapping, t: TypeMap, props: dict) -> set[URIRef]:
    cs = {t.cls} | {c for c, when in t.subclasses if all(props.get(k) == v for k, v in when.items())}
    if m.minimal_types:                                  # 다른 클래스의 상위 클래스는 추론에 맡긴다
        cs = {c for c in cs if not any(c in m.supers(d) for d in cs if d != c)}
    return cs


def export_rdf(client, m: Mapping, actor: dict | None = None) -> Graph:
    """운영 데이터를 매핑대로 RDF 로. actor 를 주면 그 실행자가 볼 수 있는 행·속성만 나간다"""
    g = Graph()
    for p, ns in m.ns.namespaces():
        g.bind(p, ns)
    visible: dict[str, set] = {}

    def node(ot: str, pk: Any) -> URIRef:
        tm = m.types[ot]
        return URIRef(m.base + tm.iri.format(**{m.onto.object_types[ot].source.primary_key: pk}))

    rows: dict[str, list[dict]] = {}
    for ot, t in m.types.items():
        q = client.objects(ot)
        if actor is not None:
            q = q.secure(actor)
        rows[ot] = [o.properties for o in q.all()]
        pk = m.onto.object_types[ot].source.primary_key
        visible[ot] = {str(r[pk]) for r in rows[ot]}
        for r in rows[ot]:
            s = node(ot, r[pk])
            for c in _types(m, t, r):
                g.add((s, RDF.type, c))
            for pm in t.props:
                for pred in pm.predicates:
                    lit = _literal(m, pred, r.get(pm.name), pm.as_)
                    if lit is not None:
                        g.add((s, pred, lit))
    for lm in m.links.values():
        lt = m.onto.link_types[lm.link_type]
        from_pk = m.onto.object_types[lt.from_].source.primary_key
        to_pk = m.onto.object_types[lt.to].source.primary_key
        pairs = []
        if lt.to_key == to_pk:                           # 외래키가 from 쪽에 있다
            pairs = [(r[from_pk], r.get(lt.from_key)) for r in rows.get(lt.from_, [])]
        else:                                            # 외래키가 to 쪽에 있다
            pairs = [(r.get(lt.to_key), r[to_pk]) for r in rows.get(lt.to, [])]
        for a, b in pairs:
            if a is None or b is None or str(a) not in visible.get(lt.from_, ()) or str(b) not in visible.get(lt.to, ()):
                continue
            fa, fb = node(lt.from_, a), node(lt.to, b)
            g.add((fa, lm.prop, fb) if lm.direction == "forward" else (fb, lm.prop, fa))
    return g


# ---------------------------------------------------------------------- 4. OWL → 운영형 정의 초안
def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _local(u: URIRef) -> str:
    return re.split(r"[#/]", str(u))[-1]


def draft_from_owl(owl: Graph) -> dict:
    """OWL 의 클래스·속성으로 MiniOnto 정의 초안을 만든다. 테이블·기본키·Action 은 OWL 에 없으므로 TODO"""
    label = lambda u: str(owl.value(u, RDFS.label) or _local(u))  # noqa: E731
    classes = sorted(c for c in owl.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef))
    defined = {c for c in classes if owl.value(c, OWL.equivalentClass) is not None}
    parent = {c: [p for p in owl.objects(c, RDFS.subClassOf) if isinstance(p, URIRef)] for c in classes}
    roots = [c for c in classes if not parent[c] and c not in defined]
    root_of = {}
    for c in classes:
        r, seen = c, set()
        while parent.get(r) and r not in seen:
            seen.add(r)
            r = parent[r][0]
        root_of[c] = r
    notes: list[str] = []
    types: dict[str, dict] = {}
    for r in roots:
        types[_local(r)] = {"display_name": label(r), "source": {"table": "TODO", "primary_key": "TODO"},
                            "properties": {}}
    # 하위 클래스 → enum 속성 제안 (서로 분리된 무리마다 하나)
    for r in roots:
        kids = [c for c in classes if r in parent[c] and c not in defined]
        groups: list[list[URIRef]] = []                 # 분리 공리로 묶인 무리 (서로 배타적인 값)
        loose: list[URIRef] = []                        # 분리 공리가 없는 하위 클래스
        for k in kids:
            g = next((g for g in groups if any((k, OWL.disjointWith, o) in owl or (o, OWL.disjointWith, k) in owl
                                               for o in g)), None)
            if g is not None:
                g.append(k)
            elif any((k, OWL.disjointWith, o) in owl or (o, OWL.disjointWith, k) in owl for o in kids):
                groups.append([k])
            else:
                loose.append(k)
        if loose:
            groups.append(loose)
        for i, g in enumerate(groups):
            name = "kind" if len(groups) == 1 else f"kind_{i + 1}"
            types[_local(r)]["properties"][name] = {"type": "enum", "values": [label(c) for c in g]}
            if g is loose and len(g) > 1:
                notes.append(f"{_local(r)}.{name}: 하위 클래스 사이에 분리 공리가 없음 — 한 객체가 여러 값을 가질 수 "
                             "있는지(목록인지) 확인")
    for c in defined:
        notes.append(f"{_local(c)}: 정의 클래스 — 객체 타입이 아니라 조건(필터·Function)으로 옮길 것")
    # 데이터 속성
    for p in sorted(owl.subjects(RDF.type, OWL.DatatypeProperty)):
        dom, rng = owl.value(p, RDFS.domain), owl.value(p, RDFS.range)
        ptype = next((t for x, ts in XSD_TYPES.items() if x == rng for t in sorted(ts) if t != "enum"), "string")
        if dom is None:
            notes.append(f"{_local(p)}: 도메인이 없어 어느 객체의 속성인지 정할 수 없음 (여러 객체에 쓰임)")
            continue
        root = root_of.get(dom, dom)
        if _local(root) in types:
            types[_local(root)]["properties"][_snake(_local(p))] = {"type": ptype}
    # 객체 속성 → 링크 (역관계 쌍은 하나로)
    links: dict[str, dict] = {}
    seen: set[URIRef] = set()
    functional = set(owl.subjects(RDF.type, OWL.FunctionalProperty))
    for p in sorted(owl.subjects(RDF.type, OWL.ObjectProperty)):
        if p in seen:
            continue
        inv = owl.value(p, OWL.inverseOf) or owl.value(predicate=OWL.inverseOf, object=p)
        seen |= {p} | ({inv} if inv else set())
        dom, rng = owl.value(p, RDFS.domain), owl.value(p, RDFS.range)
        if (dom is None or rng is None) and inv is not None:
            dom, rng = owl.value(inv, RDFS.range), owl.value(inv, RDFS.domain)
            p, inv = inv, p
        if dom is None or rng is None:
            notes.append(f"{_local(p)}: 도메인·치역이 없어 링크로 옮길 수 없음")
            continue
        many_one = p in functional
        inv_functional = inv is not None and inv in functional
        card = "many_to_one" if many_one else "one_to_many" if inv_functional else "many_to_many"
        links[_snake(_local(p))] = {"from": _local(root_of.get(dom, dom)), "to": _local(root_of.get(rng, rng)),
                                    "cardinality": card, "forward_name": _snake(_local(p)),
                                    "reverse_name": _snake(_local(inv)) if inv else "TODO"}
        if card == "many_to_many":
            notes.append(f"{_snake(_local(p))}: 함수형 표시가 없어 many_to_many 로 둠 — 실제 카디널리티를 확인할 것")
    return {"ontology": {"name": "TODO", "version": "0.0.1"}, "object_types": types, "link_types": links,
            "notes": notes}


def compare_draft(draft: dict, m: Mapping) -> dict:
    """초안이 실제 정의(매핑으로 대응)와 얼마나 맞는가"""
    out = {"객체 타입": [0, len(m.onto.object_types)], "속성(이름까지 같음)": [0, 0], "속성(대응만 됨)": [0, 0],
           "링크 타입": [0, len(m.onto.link_types)]}
    for ot, t in m.types.items():
        d = draft["object_types"].get(_local(t.cls))
        if d is None:
            continue
        out["객체 타입"][0] += 1
        for pm in t.props:
            pred = next((p for p in pm.predicates if p not in ANNOTATIONS), None)
            if pred is None:
                continue
            out["속성(대응만 됨)"][1] += 1
            out["속성(이름까지 같음)"][1] += 1
            if _snake(_local(pred)) in d["properties"]:
                out["속성(대응만 됨)"][0] += 1
                if _snake(_local(pred)) == pm.name:
                    out["속성(이름까지 같음)"][0] += 1
    draft_links = {(v["from"], v["to"]) for v in draft["link_types"].values()} | \
                  {(v["to"], v["from"]) for v in draft["link_types"].values()}
    for lm in m.links.values():
        lt = m.onto.link_types[lm.link_type]
        if (lt.from_, lt.to) in draft_links:
            out["링크 타입"][0] += 1
    return {k: tuple(v) for k, v in out.items()}


def draft_yaml(draft: dict) -> str:
    notes = draft.get("notes", [])
    body = {k: v for k, v in draft.items() if k != "notes"}
    head = "# OWL 에서 만든 MiniOnto 정의 초안 — TODO 와 아래 메모를 사람이 채운다\n" + \
        "".join(f"# - {n}\n" for n in notes)
    return head + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=120)


__all__ = ["Mapping", "Report", "Finding", "check", "axiom_checks", "export_rdf", "draft_from_owl",
           "compare_draft", "draft_yaml"]
