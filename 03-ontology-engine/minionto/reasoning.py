"""OWL 추론 결과를 업무에 쓰기 (30장)

    운영 DB ──(매핑, 엔진 경유)──▶ RDF ──(+ TBox, OWL-RL)──▶ 정의 클래스 소속
        ▲                                                        │
        └──── assign_segment / release_segment (시스템 Action) ◀─┘

원칙
1. 추론 결과를 운영 데이터에 직접 쓰지 않는다. 시스템 실행자의 Action 으로만 — 권한·실행 조건·감사 기록이 그대로.
2. 모순(OWL-RL 오류)이 있으면 아무것도 반영하지 않는다.
3. 해제는 완전한 그래프에서만. 열린 세계 가정에서 "추론되지 않음"은 "아님"이 아니다. 시스템 권한으로 전부
   내보낸 그래프(운영 데이터의 닫힌 스냅숏)일 때만, 빠진 소속을 해제로 본다.
4. 사람의 판단이 우선. 세무사가 해제한 분류는 근거가 그대로면 다시 붙이지 않는다.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

import owlrl
from rdflib import OWL, RDF, Graph, Namespace, URIRef
from rdflib.collection import Collection

from .semantic import Mapping, export_rdf

ERR = Namespace("http://www.daml.org/2002/03/agents/agent-ont#")     # owlrl 이 모순을 남기는 어휘
SYSTEM = {"id": "reasoner", "role": "시스템", "type": "system"}


@dataclass
class Inference:
    source: str                                           # 추론에 쓴 모델 (근거 모델로 남는다)
    members: dict[URIRef, dict[str, list[str]]]          # 정의 클래스 → {객체 키: 근거 ID}
    errors: list[str]                                     # OWL-RL 이 찾은 모순
    triples: tuple[int, int]                              # (추론 전, 추론 후)
    seconds: float
    complete: bool                                        # 시스템 권한으로 전부 내보낸 그래프인가
    graph: Graph = field(repr=False, default=None)        # 추론 후 그래프 (질의용)

    def count(self) -> dict[URIRef, int]:
        return {c: len(v) for c, v in self.members.items()}


def _restrictions(owl: Graph, cls: URIRef) -> list[tuple[URIRef, URIRef]]:
    """정의 클래스의 someValuesFrom 조건 → [(속성, 채움 클래스)] — 근거를 찾는 데 쓴다"""
    out = []
    for eq in owl.objects(cls, OWL.equivalentClass):
        for lst in owl.objects(eq, OWL.intersectionOf):
            for part in Collection(owl, lst):
                p, f = owl.value(part, OWL.onProperty), owl.value(part, OWL.someValuesFrom)
                if p is not None and f is not None:
                    out.append((p, f))
    return out


def _pk(m: Mapping, object_type: str, node: URIRef) -> str | None:
    tpl = m.base + m.types[object_type].iri
    pattern = "^" + re.escape(tpl).replace(re.escape("{"), "{").replace(re.escape("}"), "}")
    pattern = re.sub(r"{\w+}", "([^/]+)", pattern) + "$"
    hit = re.match(pattern, str(node))
    return hit.group(1) if hit else None


def infer(client, m: Mapping, actor: dict | None = None, extra: Graph | None = None) -> Inference:
    """운영 데이터를 내보내 OWL-RL 로 추론하고, 매핑의 inferred 클래스마다 소속 객체와 근거를 구한다

    extra: 추론 전에 덧붙일 트리플 (예제·테스트에서 모순을 일부러 넣을 때)"""
    actor = actor or SYSTEM
    t0 = time.perf_counter()
    g = export_rdf(client, m, actor=actor)
    n0 = len(g)
    g += m.owl
    if extra is not None:
        g += extra
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(g)
    errors = sorted(str(o) for s in g.subjects(RDF.type, ERR.ErrorMessage) for o in g.objects(s, ERR.error))
    members: dict[URIRef, dict[str, list[str]]] = {}
    for cls, spec in m.inferred.items():
        conds = _restrictions(m.owl, cls)
        found = {}
        for s in set(g.subjects(RDF.type, cls)):
            pk = _pk(m, spec["object_type"], s)
            if pk is None:
                continue
            found[pk] = sorted({str(o).rsplit("/", 1)[-1] for p, f in conds for o in g.objects(s, p)
                                if (o, RDF.type, f) in g})
        members[cls] = dict(sorted(found.items()))
    version = next((str(v) for v in m.owl.objects(None, OWL.versionInfo)), "")
    source = f"OWL-RL {m.name or 'mapping'} {version}".strip()
    return Inference(source, members, errors, (n0, len(g)), round(time.perf_counter() - t0, 2),
                     actor.get("type") == "system", g)


def plan_segments(client, m: Mapping, inf: Inference) -> list[dict]:
    """추론 결과와 지금의 CustomerSegment 를 비교해 할 일을 만든다 (바꾸지 않음)"""
    if inf.errors:
        raise ValueError(f"OWL 모순 {len(inf.errors)}건 — 추론 결과를 반영하지 않습니다: {inf.errors[0][:120]}")
    rows = [o.properties for o in client.objects("CustomerSegment").all()]
    active = {(r["customer_id"], r["segment"]): r for r in rows if r["status"] == "유효"}
    released = {}
    for r in sorted(rows, key=lambda r: str(r["assigned_at"])):
        if r["status"] == "해제":
            released[(r["customer_id"], r["segment"])] = r                  # 가장 최근 해제 기록
    plan = []
    for cls, spec in m.inferred.items():
        seg = spec["segment"]
        members = inf.members.get(cls, {})
        for pk, ev in members.items():
            evidence = ",".join(ev)
            if (pk, seg) in active:
                continue
            last = released.get((pk, seg))
            if last and last.get("released_by") not in (None, SYSTEM["id"]) and (last.get("evidence") or "") == evidence:
                plan.append({"op": "skip", "customer": pk, "segment": seg, "evidence": evidence,
                             "why": f"{last['released_by']} 이(가) 해제함 — 근거가 그대로"})
                continue
            plan.append({"op": "assign", "customer": pk, "segment": seg, "evidence": evidence})
        for (pk, s), r in active.items():
            if s == seg and pk not in members:
                if not inf.complete:                                         # 열린 세계: 모르는 것과 아닌 것
                    plan.append({"op": "keep", "customer": pk, "segment": seg,
                                 "why": "일부만 내보낸 그래프 — 추론되지 않았다고 해제하지 않음"})
                else:
                    plan.append({"op": "release", "customer": pk, "segment": seg, "segment_id": r["segment_id"]})
    return plan


def sync_segments(client, m: Mapping, inf: Inference, actor: dict | None = None) -> list[dict]:
    """계획대로 Action 을 실행한다. 실행자는 시스템. 같은 실행(run)을 다시 보내도 멱등 키로 한 번만"""
    actor = actor or SYSTEM
    run = uuid.uuid4().hex[:8]
    out = []
    for p in plan_segments(client, m, inf):
        if p["op"] == "assign":
            r = client.actions.apply("assign_segment", p["customer"], actor=actor,
                                     params={"segment": p["segment"], "inferred_by": inf.source,
                                             "evidence": p["evidence"] or None},
                                     idempotency_key=f"seg:{run}:{p['customer']}:{p['segment']}")
        elif p["op"] == "release":
            r = client.actions.apply("release_segment", p["segment_id"], actor=actor,
                                     params={"reason": f"추론 결과에서 빠짐 ({inf.source})"},
                                     idempotency_key=f"seg:{run}:{p['segment_id']}:release")
        else:
            out.append({**p, "status": p["op"]})
            continue
        out.append({**p, "status": r.status, "execution_id": r.execution_id, "messages": r.messages})
    return out


__all__ = ["Inference", "infer", "plan_segments", "sync_segments", "SYSTEM"]
