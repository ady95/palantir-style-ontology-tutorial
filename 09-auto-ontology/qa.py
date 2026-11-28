"""37장. Q/A 기반 생성 — 문서에서 질문·답변 쌍을 먼저 만들고, 그것으로 Action 과 테스트를 만든다

파이프라인
  1. Q/A 만들기   문서 덩어리마다 LLM → 신입 직원이 물을 질문과 문서에 근거한 답 (문서에 없으면 "문서에 없음")
                  질문마다 시스템에서 확인할 수 있는 형태(case: 실행자·대상·상태·조건·기대 결과)를 함께 적는다
  2. 정리         Q/A 전체 → 규칙과 Action 후보 (36장 추출 결과와 같은 형식, 근거는 Q/A 번호)
  3. DSL 변환     36장 build() 를 그대로 쓴다 (변환 → 검증기 → 수리)
  4. 테스트       근거가 있고 기대 결과가 있는 Q/A → 실행 가능한 판단 테스트 (check 결과와 비교)

문서에 답이 없는 질문(grounded=false)은 버리지 않는다. 37-2 에서 전문가에게 물을 질문 목록이 된다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from extract import LLM, Chunk

from minionto import OntologyClient

EXPECTS = ("가능", "불가", "승인 필요")
ACTOR_OF = {"세무사": {"id": "A001", "role": "세무사", "type": "human"},
            "실무자": {"id": "A002", "role": "실무자", "type": "human"},
            "AI비서": {"id": "agent-01", "role": "AI비서", "type": "agent"},
            "시스템": {"id": "system", "role": "시스템", "type": "system"}}

# ---------------------------------------------------------------------- 1. Q/A 만들기
QA_SYSTEM = """너는 신입 직원 교육 담당자다. 업무 문서를 읽고, 신입 직원이 시스템에서 업무를 처리하며 실제로 물어볼 질문과
문서에 근거한 답을 만든다. 결과는 JSON 하나로만 답한다.
- 데이터를 바꾸는 업무(사람이나 AI 가 시스템에서 실행하는 일)마다 질문을 만든다. 조회만 하는 일과 사무실 안내는 뺀다.
  승인하는 일과 데이터가 지켜야 할 제약은 따로 업무로 만들지 않는다. 원래 업무의 질문으로 묻는다
  (예: "AI비서가 위험 등급이 높은 고객의 신고에 자료를 요청하면 승인이 필요한가요?").
- 업무마다 다음을 빠짐없이 묻는다: 누가 할 수 있나(역할마다 따로), 어떤 상태·조건에서 할 수 있나 또는 없나,
  승인이 필요한가, 하고 나면 무엇이 바뀌나.
- 질문 하나에는 한 가지만 묻는다 (실행자 하나, 상황 하나).
- 답은 문서에 쓰인 것으로만 한다. 문서가 말하지 않으면 answer 를 "문서에 없음", grounded 를 false 로 한다. 추측해서 채우지 않는다.
- case 는 이 질문을 시스템에서 확인할 수 있게 적은 것이다.
  actor_role: 객체 모델의 역할 중 하나 (실행자를 묻지 않는 질문이면 답에 맞는 역할)
  target: 그 업무가 바꾸는 객체 타입(업무의 대상). 질문이 다른 객체의 상태를 말하면 target 을 바꾸지 말고 where 에 링크로 적는다
  target_state: 대상의 상태 값 또는 null
  where: 대상이 만족할 추가 조건식 또는 null. 형식 {"field": 속성 또는 단일링크.속성, "op": eq|ne|in|gt|gte|lt|lte|is_null|not_null, "value": 값},
         여러 개 링크는 {"some": {"link": 링크, "where": 조건식}} / {"none": {...}}, 여럿을 함께 걸면 {"all": [...]}
  expect: "가능" | "불가" | "승인 필요" | null
         할 수 있는지 묻는 질문이 아니거나 문서에 없으면 null.
         질문의 상황을 actor_role·target_state·where 로 빠짐없이 나타낼 수 없으면(개수 상한, 지금 값과 같은 값, 날짜 계산 등) null.
형식: {"qa": [{"task": 한국어 업무 이름, "question", "answer", "evidence": 근거 문장 인용 또는 null, "grounded": true|false,
              "case": {"actor_role", "target", "target_state", "where", "expect"}}]}"""


def make_qa(llm: LLM, chunk: Chunk, model_text: str) -> list[dict]:
    user = f"# 객체 모델 (이미 있는 것)\n{model_text}\n\n# 문서 ({chunk.source}: {chunk.title})\n{chunk.text}"
    out = llm.json(QA_SYSTEM, user).get("qa", [])
    return [{**q, "source": chunk.source} for q in out if isinstance(q, dict) and q.get("question")]


def number(qa: list[dict]) -> list[dict]:
    """Q/A 에 번호(Q001…)를 붙인다 — 검토 기록과 테스트가 이 번호로 Q/A 를 가리킨다"""
    return [{"id": f"Q{i:03d}", **{k: v for k, v in q.items() if k != "id"}} for i, q in enumerate(qa, 1)]


def qa_stats(qa: list[dict]) -> dict:
    grounded = [q for q in qa if q.get("grounded")]
    return {"qa": len(qa), "grounded": len(grounded), "gaps": len(qa) - len(grounded),
            "with_expect": sum(1 for q in grounded if (q.get("case") or {}).get("expect") in EXPECTS),
            "by_source": dict(Counter(q["source"] for q in qa)),
            "tasks": len({q.get("task") for q in qa})}


# ---------------------------------------------------------------------- 2. Q/A → 규칙·Action 후보
ORGANIZE_SYSTEM = """너는 질문·답변 목록에서 운영 규칙과 업무 실행 단위(Action)를 정리하는 분석가다. 결과는 JSON 하나로만 답한다.
- 질문·답변에 실제로 있는 것만 쓴다. 답이 "문서에 없음"인 Q/A 로는 규칙을 만들지 않는다.
- 같은 업무를 다르게 부른 것은 하나로 합친다. 같은 규칙을 여러 Q/A 가 말하면 규칙 하나로 쓴다.
- rules: 업무 규칙 하나당 항목 하나. statement 는 한 문장. kind 는 실행조건|권한|승인|상태전이|정책|DB제약|멱등 중 하나.
  action 은 그 규칙이 걸리는 업무 이름(actions 의 name 과 같게, 없으면 null), qa 는 근거 Q/A 번호 목록.
- actions: 데이터를 바꾸는 업무만. 승인하는 일은 업무가 아니라 원래 업무의 approval 로 쓴다. name(한국어 업무 이름), target(객체), description, roles, actor_types(human/agent/system),
  preconditions(실행 조건 문장 목록), approval(승인 규칙 문장 또는 null), transition({from: [상태], to: 상태} 또는 null),
  effects(실행하면 바뀌는 것 문장 목록), qa(이 업무에 관한 Q/A 번호 목록 — 빠짐없이)
형식: {"rules": [{"statement", "kind", "action", "qa"}], "actions": [{...}]}"""


def organize(llm: LLM, qa: list[dict], model_text: str) -> dict:
    lines = [f"{q['id']} [{q.get('task')}] Q: {q['question']} / A: {q['answer']}" for q in qa]
    out = llm.json(ORGANIZE_SYSTEM, f"# 객체 모델\n{model_text}\n\n# 질문·답변\n" + "\n".join(lines))
    src = {q["id"]: q["source"] for q in qa}
    for r in out.get("rules", []):
        kinds = {src.get(i) for i in r.get("qa") or [] if i in src}
        r["source"] = "both" if len(kinds) > 1 else (kinds.pop() if kinds else "manual")
        r["evidence"] = ", ".join(r.get("qa") or [])
    for a in out.get("actions", []):
        kinds = {src.get(i) for i in a.get("qa") or [] if i in src}
        a["source"] = "both" if len(kinds) > 1 else (kinds.pop() if kinds else "manual")
    return {"rules": [r for r in out.get("rules", []) if r.get("statement")],
            "actions": [a for a in out.get("actions", []) if a.get("name")]}


# ---------------------------------------------------------------------- 4. Q/A → 판단 테스트
@dataclass
class QATest:
    id: str                      # Q/A 번호
    task: str                    # 한국어 업무 이름 (Q/A 의 task)
    actor_role: str
    target: str
    target_state: str | None
    where: dict | None
    expect: str                  # 가능 | 불가 | 승인 필요
    question: str = ""


def build_tests(qa: list[dict]) -> list[QATest]:
    """근거가 있고(grounded) 기대 결과가 있는 Q/A 만 테스트가 된다"""
    out = []
    for q in qa:
        c = q.get("case") or {}
        if q.get("grounded") and c.get("expect") in EXPECTS and c.get("actor_role") in ACTOR_OF and c.get("target"):
            out.append(QATest(q["id"], q.get("task") or "", c["actor_role"], c["target"], c.get("target_state"),
                              c.get("where") or None, c["expect"], q["question"]))
    return out


def task_map(qa: list[dict], actions: dict[str, dict]) -> dict[str, str]:
    """Q/A 번호 → Action 이름. actions 는 이름 → {candidate: {qa: [...]}} (36장 build() 결과)"""
    out: dict[str, str] = {}
    for name, entry in actions.items():
        for i in (entry.get("candidate") or {}).get("qa") or []:
            out.setdefault(i, name)
    # Action 에 묶이지 않은 Q/A(주로 "문서에 없음")는 같은 업무 이름의 Q/A 가 가장 많이 묶인 Action 으로
    task_of = {q["id"]: q.get("task") for q in qa}
    votes: dict[str, Counter] = {}
    for i, name in out.items():
        votes.setdefault(task_of.get(i), Counter())[name] += 1
    for q in qa:
        if q["id"] not in out and q.get("task") in votes:
            out[q["id"]] = votes[q["task"]].most_common(1)[0][0]
    return out


def test_params(c: OntologyClient, action: str, today: str = "2027-01-18") -> dict:
    """판단에 넣을 입력. 객체를 가리키는 입력(객체 타입, 또는 다른 객체의 기본키 이름을 가진 문자열)은 실제 객체 키를 쓴다"""
    onto = c.onto
    pks = {ot.source.primary_key: n for n, ot in onto.object_types.items()}
    out: dict[str, Any] = {}
    for n, p in onto.actions[action].parameters.items():
        otype = p.object_type if p.type == "object" else (pks.get(n) if p.type == "string" else None)
        if otype:
            o = c.objects(otype).order_by(onto.object_types[otype].source.primary_key).first()
            out[n] = o.pk if o else None
        elif p.type == "enum":
            out[n] = p.values[-1]
        else:
            out[n] = {"integer": 1, "decimal": 1, "float": 1.0, "boolean": True, "date": today}.get(p.type, "테스트")
    return out


OK_STATUS = {"ready": "가능", "approval_required": "승인 필요", "denied": "불가(권한)", "rejected": "불가(조건)"}


@dataclass
class CaseResult:
    test: QATest
    action: str | None
    outcome: str                 # pass | fail | skip
    reason: str = ""
    seen: dict[str, int] = field(default_factory=dict)       # 판단 결과별 대상 수


def run_test(c: OntologyClient, t: QATest, action: str | None, params: dict, n: int = 50) -> CaseResult:
    """조건에 맞는 대상을 최대 n 개 골라 check() 한다.
    가능·승인 필요: 그런 대상이 하나라도 있으면 통과 (승인 필요는 바로 실행되는 대상이 없어야 함).
                    실행 조건에 막힌 대상만 있고 실행되는 대상이 없으면 판정 보류(skip) — 조건을 모두 갖춘 대상이
                    데이터에 없는 것 (권한에만 막혔다면 실패: 역할 검사는 조건보다 먼저라 모든 대상이 막힌다)
    불가: 고른 대상 모두에서 실행할 수 없어야 통과"""
    onto = c.onto
    if action is None or action not in onto.actions:
        return CaseResult(t, action, "fail", "이 업무에 해당하는 Action 이 없음")
    if onto.actions[action].target != t.target:
        return CaseResult(t, action, "fail", f"Action 의 대상이 다름 ({onto.actions[action].target}, 질문은 {t.target})")
    try:
        s = c.objects(t.target)
        sm = onto.object_types[t.target].state_machine
        if t.target_state and sm:
            s = s.where({"field": sm.property, "op": "eq", "value": t.target_state})
        if t.where:
            s = s.where(t.where)
        objs = s.order_by(onto.object_types[t.target].source.primary_key).limit(n).all()
    except Exception as e:                                       # noqa: BLE001 — 조건식이 잘못된 Q/A
        return CaseResult(t, action, "skip", f"대상 조건을 실행할 수 없음: {str(e).splitlines()[0][:80]}")
    if not objs:
        return CaseResult(t, action, "skip", "조건에 맞는 대상이 데이터에 없음")
    seen = Counter()
    for o in objs:
        st = c.actions.check(action, o.pk, actor=ACTOR_OF[t.actor_role], params=params).status
        seen[OK_STATUS.get(st, st)] += 1
    if t.expect == "불가":
        ok = set(seen) <= {"불가(권한)", "불가(조건)"}
    else:
        ok = seen[t.expect] > 0 and (t.expect == "가능" or seen["가능"] == 0)
        if not ok and seen["불가(조건)"] and not seen["가능"] and not seen["승인 필요"]:
            return CaseResult(t, action, "skip", "실행 조건을 모두 갖춘 대상이 데이터에 없음", dict(seen))
    return CaseResult(t, action, "pass" if ok else "fail", "" if ok else f"기대 {t.expect}, 실제 {dict(seen)}",
                      dict(seen))


def run_tests(c: OntologyClient, tests: list[QATest], qa_to_action: dict[str, str],
              params_of: Any) -> list[CaseResult]:
    """params_of(action 이름) → 판단에 넣을 입력"""
    out = []
    for t in tests:
        a = qa_to_action.get(t.id)
        out.append(run_test(c, t, a, params_of(a) if a in c.onto.actions else {}))
    return out


def summarize(results: list[CaseResult]) -> dict:
    n = Counter(r.outcome for r in results)
    return {"tests": len(results), "pass": n["pass"], "fail": n["fail"], "skip": n["skip"],
            "fails": [f"{r.test.id} {r.action}: {r.test.question} — {r.reason}" for r in results if r.outcome == "fail"]}


def dump_tests(results: list[CaseResult]) -> list[dict]:
    return [{"id": r.test.id, "action": r.action, "actor": r.test.actor_role, "target_state": r.test.target_state,
             "where": r.test.where, "expect": r.test.expect, "outcome": r.outcome, "reason": r.reason,
             "question": r.test.question} for r in results]


__all__ = ["test_params", "make_qa", "number", "qa_stats", "organize", "QATest", "build_tests", "task_map", "run_test", "run_tests",
           "summarize", "dump_tests", "ACTOR_OF", "EXPECTS"]
