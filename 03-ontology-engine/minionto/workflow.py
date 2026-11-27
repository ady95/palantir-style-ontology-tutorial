"""업무 그래프 — 신고 지연 위험 고객 처리 에이전트 (27장, LangGraph)

    탐지(detect) → 분석(analyze) → 메모 작성(draft, LLM) → 실행(act) → … 다음 대상 …
                → 승인 대기(wait_approval, 중단·재개) → 확인(verify) → 보고(report, LLM)

무엇을 누가 정하는가.
- 그래프(코드): 순서, 분기, 반복, 중단. 어떤 신고를 다룰지(위험 점수), 실행 결과(status)에 따라 어디로 갈지.
- 온톨로지(엔진): 할 수 있는지(권한·실행 조건·상태 전이), 승인이 필요한지, 중복 실행인지.
- LLM: 말로 써야 하는 것만 — 고객에게 보낼 메모, 담당자에게 줄 요약. 결과는 그래프가 검사한다.

도구는 25장의 AgentSession 을 그대로 쓴다. 그래서 에이전트 실행자의 권한·정책, 읽은 값으로 expect,
업무 단위 멱등 키(agent:<업무 ID>:<Action>:<대상>)가 그래프에도 똑같이 적용된다.

그래프 상태는 체크포인터(PostgresSaver)에, 도구 세션(읽은 상태·멱등 키)은 SessionStore(26장)에 둔다.
둘 다 업무 ID(thread_id)를 키로 쓰므로, 프로세스가 죽거나 승인을 며칠 기다려도 같은 업무를 이어 간다.
승인 결과는 재개할 때 넘긴 값이 아니라 엔진의 승인 기록(approval_requests)에서 읽는다.
"""

from __future__ import annotations

import json
import operator
import re
from contextlib import contextmanager
from typing import Annotated, Any, Callable, Iterator, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .actions import ENGINE_SCHEMA
from .agent import AgentSession, SessionStore
from .audit import AuditLog
from .objects import OntologyClient

REQ = "request_missing_documents"
DONE = {"completed", "approval_required"}           # 엔진이 받아들인 결과


class State(TypedDict, total=False):
    task_id: str                                      # 업무 ID = thread_id = 도구 세션 키
    as_of: str                                        # 기준일 (재개할 때 같은 날짜로 연다)
    min_score: int                                    # 이 점수 이상인 고객만 다룬다
    doc_names: dict[str, str]                         # 자료 코드 → 이름
    items: list[dict]                                 # 대상 신고와 처리 결과 (노드가 통째로 바꾼다)
    cursor: int                                       # 지금 처리 중인 items 의 위치
    log: Annotated[list[dict], operator.add]          # 노드 실행 기록 (덧붙이기만)
    report: dict
    summary: str


# ---------------------------------------------------------------------- 문장을 쓰는 쪽 (LLM 또는 템플릿)
class TemplateWriter:
    """LLM 없이 정해진 문장으로 — 테스트와 비교용"""
    name = "template"

    def note(self, item: dict) -> str:
        return f"{item['due_date']} 신고 기한까지 {', '.join(item['missing'])} 자료를 보내 주세요."

    def summary(self, report: dict) -> str:
        c = report["counts"]
        return " · ".join(f"{k} {v}건" for k, v in c.items())


class LLMWriter:
    """OpenAI 호환 API 로 메모와 요약을 쓴다. 판단은 하지 않는다 — 그래프가 준 사실만 문장으로"""
    name = "llm"

    NOTE = ("세무사무소가 고객에게 보내는 자료 요청 메모를 한국어 두 문장 이내, 200자 이내로 써라. "
            "주어진 자료 이름과 기한만 쓰고, 없는 사실(금액·가산세 액수·전화번호 등)은 지어내지 마라. "
            "인사말·서명·따옴표 없이 본문만 출력한다.")
    SUMMARY = ("세무사무소 담당 세무사에게 보내는 업무 처리 보고를 한국어로 5줄 이내로 써라. "
               "주어진 집계와 목록에 있는 숫자·ID 만 쓰고, 사람이 해야 할 일을 먼저 쓴다.")

    def __init__(self, chat):
        self.chat = chat
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def _ask(self, system: str, user: str, max_tokens: int) -> str:
        resp = self.chat.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              max_tokens=max_tokens)
        self.usage["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens"):
            self.usage[k] += resp.get("usage", {}).get(k, 0)
        return (resp["choices"][0]["message"].get("content") or "").strip()

    def note(self, item: dict) -> str:
        facts = (f"상호: {item['company_name']}\n세목·기간: {item['tax_kind']} {item['period']}\n"
                 f"신고 기한: {item['due_date']} ({item['days_left']}일 남음)\n"
                 f"아직 받지 못한 자료: {', '.join(item['missing'])}")
        return self._ask(self.NOTE, facts, 200)

    def summary(self, report: dict) -> str:
        return self._ask(self.SUMMARY, json.dumps(report, ensure_ascii=False), 400)


def check_note(text: str, item: dict, limit: int = 200) -> list[str]:
    """LLM 이 쓴 메모를 그래프가 검사한다 — 통과하지 못하면 템플릿으로 바꾼다"""
    problems = []
    if not text:
        problems.append("빈 메모")
    if len(text) > limit:
        problems.append(f"{limit}자 초과({len(text)}자)")
    if re.search(r"\d{2,4}-\d{3,4}-\d{4}|\d{1,3}(,\d{3})+원", text):
        problems.append("주지 않은 전화번호·금액")
    if item["missing"] and not any(m in text for m in item["missing"]):
        problems.append("요청할 자료 이름이 없음")
    return problems


def check_summary(text: str, report: dict) -> list[str]:
    """요약에 보고에 없는 신고 ID 가 있으면 안 된다"""
    known = set(re.findall(r"R\d{5}", json.dumps(report, ensure_ascii=False)))
    extra = sorted(set(re.findall(r"R\d{5}", text)) - known)
    return ([] if text else ["빈 요약"]) + ([f"보고에 없는 ID {', '.join(extra)}"] if extra else [])


# ---------------------------------------------------------------------- 그래프
class FilingRiskWorkflow:
    """신고 지연 위험 고객 처리 업무. 실행자는 에이전트(예: agent-01, AI비서)"""

    def __init__(self, client: OntologyClient, actor: dict, *, writer=None, min_score: int = 50,
                 max_retries: int = 2, on_act: Callable[[dict, dict], None] | None = None):
        """on_act: 실행 직후·체크포인트 저장 전에 부르는 훅 (테스트에서 프로세스 중단을 흉내 낸다)"""
        self.client, self.actor = client, dict(actor)
        self.writer = writer or TemplateWriter()
        self.min_score, self.max_retries, self.on_act = min_score, max_retries, on_act
        self.store = SessionStore(client)

    # ------------------------------------------------------------------ 도구 세션
    @contextmanager
    def session(self, state: State) -> Iterator[AgentSession]:
        """업무 ID 의 도구 세션을 열고, 노드가 정상으로 끝나면 저장한다 (예외면 저장하지 않는다)"""
        key = state["task_id"]
        s = self.store.open(key, self.actor, max_rows=500, max_calls=10_000)
        yield s
        self.store.save(key, s)

    # ------------------------------------------------------------------ 노드
    def detect(self, state: State) -> dict:
        """자료수집중 신고의 고객마다 filing_delay_risk 를 계산해 기준 이상만 대상으로"""
        min_score = state.get("min_score", self.min_score)
        with self.session(state) as s:
            names = {r["doc_type_code"]: r["doc_name"]
                     for r in s.call("find_objects", {"object_type": "DocumentType"})["rows"]}
            found = s.call("find_objects", {"object_type": "TaxReturn", "order_by": "return_id",
                                            "where": [{"field": "status", "op": "eq", "value": "자료수집중"}]})
            risk: dict[str, dict] = {}
            items = []
            for r in found["rows"]:
                cid = r["customer_id"]
                if cid not in risk:
                    risk[cid] = s.call("call_function", {"name": "filing_delay_risk",
                                                         "args": {"customer": cid}})["result"]
                k = risk[cid]
                if k["score"] >= min_score:
                    items.append({"return_id": r["return_id"], "customer_id": cid, "score": k["score"],
                                  "level": k["level"], "reasons": k["reasons"], "status": "new"})
        items.sort(key=lambda i: (-i["score"], i["return_id"]))
        return {"items": items, "cursor": 0, "min_score": min_score, "doc_names": names,
                "log": [{"node": "detect", "returns": found["count"], "returned": found["returned"],
                         "customers": len(risk), "targets": len(items)}]}

    def analyze(self, state: State) -> dict:
        """대상을 읽고(읽은 상태 = expect), 미제출 자료를 보고, 지금 할 수 있는지 판단만 한다"""
        items, i = [dict(x) for x in state["items"]], state["cursor"]
        it = items[i]
        with self.session(state) as s:
            ret = s.call("get_object", {"object_type": "TaxReturn", "id": it["return_id"]})["object"]
            cust = s.call("get_object", {"object_type": "Customer", "id": it["customer_id"]})["object"]
            docs = s.call("call_function", {"name": "missing_documents",
                                            "args": {"tax_return": it["return_id"]}})["result"]
            chk = s.call("check_action", {"action": REQ, "target": it["return_id"]})
        names = state.get("doc_names", {})
        it.update(company_name=cust.get("company_name"), customer_risk=cust.get("risk_level"),
                  tax_kind=ret["tax_kind"], period=ret["period"], due_date=str(ret["due_date"]),
                  days_left=(_date(ret["due_date"]) - self.client.today).days,
                  missing=[names.get(d["doc_type_code"], d["doc_type_code"]) for d in docs
                           if d["status"] == "미요청"],
                  requested=sum(1 for d in docs if d["status"] == "요청됨"),
                  check=chk["status"], check_messages=chk.get("messages", []))
        if chk["status"] in ("ready", "approval_required"):
            it["route"] = "act"
        else:                                         # 규칙상 지금은 할 수 없다 → 사람에게 넘긴다
            it.update(route="handoff", status="handoff", messages=chk.get("messages", []))
        items[i] = it
        return {"items": items, "log": [{"node": "analyze", "return_id": it["return_id"], "check": chk["status"]}]}

    def draft(self, state: State) -> dict:
        """LLM 이 메모를 쓰고, 그래프가 검사한다"""
        items, i = [dict(x) for x in state["items"]], state["cursor"]
        it = items[i]
        text, by = "", self.writer.name
        try:
            text = self.writer.note(it)
            problems = check_note(text, it)
        except Exception as e:  # noqa: BLE001 — LLM 장애는 업무를 멈추지 않는다
            problems = [f"작성 실패: {type(e).__name__}: {str(e)[:60]}"]
        if problems:
            text, by = TemplateWriter().note(it), "template"
        it.update(note=text, note_by=by, note_problems=problems or None)
        items[i] = it
        return {"items": items, "log": [{"node": "draft", "return_id": it["return_id"], "by": by,
                                         "problems": problems}]}

    def act(self, state: State) -> dict:
        """실행한다. 같은 업무 ID 로 다시 오면 멱등 키 때문에 처음 결과를 돌려받는다"""
        items, i = [dict(x) for x in state["items"]], state["cursor"]
        it = items[i]
        with self.session(state) as s:
            out = s.call("run_action", {"action": REQ, "target": it["return_id"], "params": {"note": it["note"]}})
            if self.on_act:
                self.on_act(it, out)
        status = out.get("original_status", out["status"]) if out["status"] == "replayed" else out["status"]
        it["attempts"] = it.get("attempts", 0) + 1
        it.update(status=status, messages=out.get("messages", []), approval_id=out.get("approval_id"),
                  execution_id=out.get("execution_id"), idempotency_key=out.get("idempotency_key"),
                  replayed=out["status"] == "replayed" or None)
        if status == "rejected":                      # 판단 뒤에 상황이 바뀌었다 → 사람에게
            it["status"] = "handoff"
        items[i] = it
        return {"items": items, "log": [{"node": "act", "return_id": it["return_id"], "status": out["status"]}]}

    def next_item(self, state: State) -> dict:
        return {"cursor": state["cursor"] + 1}

    def wait_approval(self, state: State) -> Command[Literal["wait_approval", "verify"]]:
        """승인 대기 — 엔진의 승인 기록과 맞춘 뒤, 남은 것이 있으면 멈춘다 (재개하면 이 노드를 처음부터 다시)"""
        items = self.reconcile(state["items"])
        waiting = [i for i in items if i["status"] == "approval_required"]
        log = [{"node": "wait_approval", "waiting": len(waiting)}]
        if not waiting:
            return Command(update={"items": items, "log": log}, goto="verify")
        answer = interrupt({"task_id": state["task_id"], "message": f"승인 대기 {len(waiting)}건",
                            "waiting": [{"approval_id": i["approval_id"], "return_id": i["return_id"],
                                         "customer_id": i["customer_id"], "note": i.get("note")}
                                        for i in waiting]})
        if isinstance(answer, dict) and answer.get("stop"):     # 더 기다리지 않고 보고한다 (요청은 남아 있다)
            for i in items:
                if i["status"] == "approval_required":
                    i["status"] = "still_waiting"
            return Command(update={"items": items, "log": log}, goto="verify")
        return Command(update={"items": items, "log": log}, goto="wait_approval")

    def reconcile(self, items: list[dict]) -> list[dict]:
        """승인 대기 항목을 엔진의 승인 기록으로 갱신한다 — 그래프가 기억하는 값보다 DB 가 우선"""
        items = [dict(x) for x in items]
        for it in items:
            if it["status"] not in ("approval_required", "still_waiting") or not it.get("approval_id"):
                continue
            row = self.client.conn.execute(
                f"""SELECT status, execution_id, comment, decided_by->>'id' AS decided_by
                    FROM {ENGINE_SCHEMA}.approval_requests WHERE approval_id = %s""",
                (it["approval_id"],)).fetchone()
            if row is None or row["status"] == "pending":
                continue
            it.update(status={"approved": "completed", "rejected": "rejected_by_approver",
                              "expired": "expired"}[row["status"]],
                      decided_by=row["decided_by"], comment=row["comment"],
                      execution_id=str(row["execution_id"]) if row["execution_id"] else it.get("execution_id"))
        return items

    def verify(self, state: State) -> dict:
        """완료라고 기록한 항목을 실행 기록(action_log)과 객체 상태로 확인한다"""
        items = self.reconcile(state["items"])
        audit = AuditLog(self.client)
        problems = 0
        with self.session(state) as s:
            for it in items:
                if it["status"] != "completed":
                    continue
                t = audit.trace(it["execution_id"]) if it.get("execution_id") else {}
                ex = t.get("execution") or {}
                docs = s.call("call_function", {"name": "missing_documents",
                                                "args": {"tax_return": it["return_id"]}})["result"]
                left = [d["doc_type_code"] for d in docs if d["status"] == "미요청"]
                why = []
                if ex.get("status") != "completed":
                    why.append("실행 기록 없음" if not ex else f"실행 기록 상태 {ex.get('status')}")
                elif ex["actor"].get("id") != self.actor["id"]:
                    why.append(f"실행자 {ex['actor'].get('id')}")
                if left:
                    why.append(f"미요청 자료 {len(left)}건 남음")
                it["verified"] = not why
                if why:
                    it["verify_problems"] = why
                    problems += 1
        return {"items": items, "log": [{"node": "verify", "problems": problems}]}

    def report(self, state: State) -> dict:
        items = state.get("items", [])
        counts: dict[str, int] = {}
        for it in items:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        handoff: dict[str, list[str]] = {}
        for it in items:
            if it["status"] == "handoff":
                handoff.setdefault(" / ".join(it.get("messages") or ["-"]), []).append(it["return_id"])
        rep = {"as_of": state.get("as_of"), "min_score": state.get("min_score"), "targets": len(items),
               "counts": counts,
               "completed": [i["return_id"] for i in items if i["status"] == "completed"],
               "approval": {i["return_id"]: i["status"] for i in items if i.get("approval_id")},
               "handoff_by_reason": handoff,
               "unverified": [i["return_id"] for i in items if i.get("verified") is False]}
        try:
            summary = self.writer.summary(rep)
            problems = check_summary(summary, rep)
        except Exception as e:  # noqa: BLE001
            summary, problems = "", [f"작성 실패: {type(e).__name__}: {str(e)[:60]}"]
        if problems:
            summary = TemplateWriter().summary(rep)
        return {"report": rep, "summary": summary, "log": [{"node": "report", "problems": problems}]}

    # ------------------------------------------------------------------ 분기
    @staticmethod
    def after_detect(state: State) -> str:
        return "analyze" if state["items"] else "report"

    @staticmethod
    def after_analyze(state: State) -> str:
        return "draft" if state["items"][state["cursor"]].get("route") == "act" else "next_item"

    def after_act(self, state: State) -> str:
        it = state["items"][state["cursor"]]
        if it["status"] == "stale" and it["attempts"] <= 1:
            return "analyze"                          # 그사이 바뀌었다 → 다시 읽고 판단 (한 번만)
        if it["status"] == "retry" and it["attempts"] <= self.max_retries:
            return "act"                              # 일시적 오류 → 같은 멱등 키로 다시
        return "next_item"

    @staticmethod
    def after_item(state: State) -> str:
        if state["cursor"] < len(state["items"]):
            return "analyze"
        return "wait_approval"

    # ------------------------------------------------------------------ 조립
    def build(self, checkpointer=None, **compile_kw):
        g = StateGraph(State)
        for name in ("detect", "analyze", "draft", "act", "next_item", "wait_approval", "verify", "report"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "detect")
        g.add_conditional_edges("detect", self.after_detect, ["analyze", "report"])
        g.add_conditional_edges("analyze", self.after_analyze, ["draft", "next_item"])
        g.add_edge("draft", "act")
        g.add_conditional_edges("act", self.after_act, ["analyze", "act", "next_item"])
        g.add_conditional_edges("next_item", self.after_item, ["analyze", "wait_approval"])
        g.add_edge("verify", "report")
        g.add_edge("report", END)
        return g.compile(checkpointer=checkpointer, **compile_kw)


def _date(v: Any):
    from datetime import date
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


# ---------------------------------------------------------------------- 실행 도우미
def config(task_id: str, recursion_limit: int = 1000) -> dict:
    """recursion_limit: 한 번 실행에서 허용하는 단계(super-step) 수. 노드 한 번이 한 단계이고 대상 37건에
    약 90단계였다. 기본값(LangGraph 1.1.4 이상 10,007)보다 작게 잡아, 분기 실수로 생긴 반복을 일찍 멈춘다"""
    return {"configurable": {"thread_id": task_id}, "recursion_limit": recursion_limit}


def postgres_saver(url: str):
    """그래프 체크포인트를 실습 DB 의 minionto 스키마에 둔다 (load_taxos.sh 로 함께 초기화된다)"""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {ENGINE_SCHEMA}")
    conn = psycopg.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row,
                           options=f"-c search_path={ENGINE_SCHEMA}")
    saver = PostgresSaver(conn)
    saver.setup()
    return saver


def interrupts(result: dict) -> list[Any]:
    return [i.value for i in result.get("__interrupt__", [])]


__all__ = ["FilingRiskWorkflow", "LLMWriter", "TemplateWriter", "check_note", "config", "postgres_saver",
           "interrupts", "REQ"]
