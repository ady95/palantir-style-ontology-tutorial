"""34장. 상담 응대 에이전트 — 문서 지식 + 업무 데이터

문의 하나를 처리하는 순서
1. 규칙으로 먼저 거른다   — 휴업·폐업 고객, 엔진이 AI 답변을 막는 문의(세금계산·담당자확인)는 바로 담당자에게
2. 업무 데이터를 모은다   — 에이전트 권한(AgentSession)으로 고객·현재 신고·미제출 자료·진행 중 업무
3. 문서 지식을 찾는다     — 안내 문서(knowledge/*.md)를 임베딩으로 검색 (임베딩 API 가 없으면 단어 겹침)
4. 답을 쓴다             — LLM 이 업무 데이터와 문서만으로 답하거나 "넘김"을 고른다 (LLM 없이 쓰는 템플릿도 있다)
5. 답을 검사한다         — 금액·주지 않은 날짜가 있으면 넘긴다
6. 실행한다              — answer_inquiry / escalate_inquiry (엔진이 다시 검사)
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from minionto.agent import AgentSession

HERE = Path(__file__).resolve().parent
KNOWLEDGE = HERE / "knowledge"


# ---------------------------------------------------------------------- 3. 문서 지식
@dataclass
class Doc:
    id: str
    title: str
    text: str
    vec: list[float] | None = None


class Knowledge:
    def __init__(self, folder: Path = KNOWLEDGE, embed: bool = True):
        self.docs = []
        for p in sorted(folder.glob("K*.md")):
            lines = p.read_text(encoding="utf-8").strip().splitlines()
            self.docs.append(Doc(p.stem.split("-")[0], lines[0].lstrip("# ").strip(), "\n".join(lines[1:]).strip()))
        self.base = (os.getenv("EMBED_BASE_URL") or "").rstrip("/")
        self.model = os.getenv("EMBED_SERVER_MODEL") or os.getenv("EMBED_MODEL") or ""
        self.mode = "keyword"
        if embed and self.base and self.model:
            try:
                vecs = self._embed([f"{d.title}\n{d.text}" for d in self.docs])
                for d, v in zip(self.docs, vecs):
                    d.vec = v
                self.mode = "embedding"
            except Exception:  # noqa: BLE001 — 임베딩 API 가 없으면 단어 겹침으로
                self.mode = "keyword"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        url = self.base + ("/embeddings" if self.base.endswith("/v1") else "/v1/embeddings")
        r = httpx.post(url, json={"model": self.model, "input": texts}, timeout=60)
        r.raise_for_status()
        return [x["embedding"] for x in sorted(r.json()["data"], key=lambda x: x["index"])]

    def search(self, question: str, k: int = 2) -> list[Doc]:
        if self.mode == "embedding":
            q = self._embed([question])[0]
            score = {d.id: _cos(q, d.vec) for d in self.docs}
        else:
            words = set(re.findall(r"[가-힣A-Za-z]{2,}", question))
            score = {d.id: len(words & set(re.findall(r"[가-힣A-Za-z]{2,}", d.title + d.text))) for d in self.docs}
        return sorted(self.docs, key=lambda d: -score[d.id])[:k]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# ---------------------------------------------------------------------- 2. 업무 데이터
def facts(s: AgentSession, inquiry: dict) -> dict:
    """에이전트 권한으로 읽은 고객 상황 — 감춘 속성(대표자·전화 등)은 들어오지 않는다"""
    cid = inquiry["customer_id"]
    cust = s.call("get_object", {"object_type": "Customer", "id": cid})["object"]
    out = {"customer_id": cid, "company_name": cust.get("company_name"), "business_status": cust.get("business_status"),
           "vat_type": cust.get("vat_type"), "business_type": cust.get("business_type")}
    rets = s.call("find_objects", {"object_type": "TaxReturn", "order_by": "-due_date", "limit": 1, "where": [
        {"field": "customer_id", "op": "eq", "value": cid}, {"field": "status", "op": "eq", "value": "자료수집중"}]})
    if rets.get("rows"):
        r = rets["rows"][0]
        docs = s.call("call_function", {"name": "missing_documents", "args": {"tax_return": r["return_id"]}}).get("result", [])
        names = {d["doc_type_code"]: d["doc_name"] for d in s.call("find_objects", {"object_type": "DocumentType"}).get("rows", [])}
        got = s.call("find_objects", {"object_type": "DocumentSubmission", "where": [
            {"field": "return_id", "op": "eq", "value": r["return_id"]},
            {"field": "status", "op": "eq", "value": "제출완료"}]}).get("rows", [])
        out["current_return"] = {"return_id": r["return_id"], "tax_kind": r["tax_kind"], "period": r["period"],
                                 "due_date": str(r["due_date"]), "status": r["status"],
                                 "not_received": [{"doc": names.get(d["doc_type_code"]), "status": d["status"]}
                                                  for d in docs],
                                 "received": [names.get(d["doc_type_code"]) for d in got]}   # 받은 자료도 (실측 뒤 추가)
    else:
        out["current_return"] = None
    return out


# ---------------------------------------------------------------------- 4. 답 쓰기
SYSTEM = """너는 세무사무소의 상담 응대 비서다. 고객 문의에 한국어로 짧게(세 문장 이내) 답한다.
- 아래 '고객 상황'과 '안내 문서'에 있는 내용만 쓴다. 없는 사실, 금액, 날짜를 지어내지 않는다.
- 세금·가산세 금액, 고객별 세무 판단, 담당자 변경, 과거 이력에 대한 평가는 답하지 말고 넘긴다.
- 답할 수 없으면 decision 을 escalate 로 하고 reason 에 이유를 쓴다.
JSON 하나만 출력한다: {"decision": "answer" 또는 "escalate", "answer": "...", "reason": "...", "docs": ["K01", ...]}"""


class LLMResponder:
    name = "llm"

    def __init__(self, chat):
        self.chat = chat
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def respond(self, inquiry: dict, fx: dict, docs: list[Doc]) -> dict:
        user = (f"문의: {inquiry['question']}\n\n고객 상황(JSON): {json.dumps(fx, ensure_ascii=False)}\n\n안내 문서:\n"
                + "\n\n".join(f"[{d.id}] {d.title}\n{d.text}" for d in docs))
        resp = self.chat.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                              max_tokens=400)
        self.usage["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens"):
            self.usage[k] += resp.get("usage", {}).get(k, 0)
        text = (resp["choices"][0]["message"].get("content") or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else {"decision": "escalate", "reason": "형식 오류"}
        except json.JSONDecodeError:
            return {"decision": "escalate", "reason": "형식 오류"}


class TemplateResponder:
    """LLM 없이: 미제출 자료·신고 기한은 업무 데이터로, 그 밖은 가장 가까운 문서의 첫 줄로"""
    name = "template"

    def respond(self, inquiry: dict, fx: dict, docs: list[Doc]) -> dict:
        cur = fx.get("current_return")
        if inquiry["category"] == "신고일정" and cur:
            return {"decision": "answer", "answer": f"이번 {cur['tax_kind']} 신고 기한은 {cur['due_date']}입니다.",
                    "docs": [docs[0].id]}
        if inquiry["category"] == "자료제출" and cur and cur["not_received"]:
            items = ", ".join(f"{d['doc']}({d['status']})" for d in cur["not_received"])
            return {"decision": "answer", "answer": f"아직 받지 못한 자료는 {items}입니다. {cur['due_date']} 신고 기한 전에 보내 주세요.",
                    "docs": [docs[0].id]}
        if docs:
            return {"decision": "answer", "answer": docs[0].text.splitlines()[0].lstrip("- "), "docs": [docs[0].id]}
        return {"decision": "escalate", "reason": "안내할 문서가 없음"}


# ---------------------------------------------------------------------- 5. 답 검사
def check_answer(answer: str, fx: dict, docs: list[Doc] | None = None) -> list[str]:
    """금액·비율, 그리고 업무 데이터(신고 기한)나 안내 문서(예: 7월 1일)에 없는 날짜가 있으면 문제"""
    problems = []
    if not answer:
        problems.append("빈 답변")
    if re.search(r"\d[\d,]*\s*(원|만\s*원|%)", answer):
        problems.append("금액·비율이 들어 있음")
    allowed = {fx["current_return"]["due_date"][5:]} if fx.get("current_return") else set()   # 월-일
    for d in docs or []:
        allowed |= {f"{int(m):02d}-{int(dd):02d}" for m, dd in re.findall(r"(\d{1,2})월\s*(\d{1,2})일", d.text)}
    for y, mth, d in re.findall(r"(20\d\d)[년.\-]\s*(\d{1,2})[월.\-]\s*(\d{1,2})", answer):
        if f"{int(mth):02d}-{int(d):02d}" not in allowed:
            problems.append(f"주지 않은 날짜 {y}-{mth}-{d}")
    return problems


# ---------------------------------------------------------------------- 1·6. 규칙과 실행
@dataclass
class Result:
    inquiry_id: str
    category: str
    route: str                     # answered | escalated
    by: str                        # rule | engine | llm | check | template
    answer: str = ""
    reason: str = ""
    docs: list[str] = field(default_factory=list)
    status: str = ""


def handle(s: AgentSession, kb: Knowledge, responder, inquiry: dict) -> Result:
    iid = inquiry["inquiry_id"]
    s.call("get_object", {"object_type": "Inquiry", "id": iid})
    fx = facts(s, inquiry)

    def escalate(reason: str, by: str) -> Result:
        out = s.call("run_action", {"action": "escalate_inquiry", "target": iid, "params": {"reason": reason}})
        return Result(iid, inquiry["category"], "escalated", by, reason=reason, status=out["status"])

    if fx["business_status"] != "영업" or inquiry["category"] == "휴폐업":
        return escalate("휴업·폐업 고객의 문의 — 담당자가 상황을 확인해 안내", "rule")
    chk = s.call("check_action", {"action": "answer_inquiry", "target": iid, "params": {"answer": "-"}})
    if chk["status"] != "ready":
        return escalate("엔진: " + " / ".join(chk.get("messages", [])), "engine")
    docs = kb.search(inquiry["question"])
    try:
        r = responder.respond(inquiry, fx, docs)
    except Exception as e:  # noqa: BLE001
        return escalate(f"답변 생성 실패 ({type(e).__name__})", responder.name)
    if r.get("decision") != "answer":
        return escalate(r.get("reason") or "답할 수 없음", responder.name)
    problems = check_answer(r.get("answer", ""), fx, docs)
    if problems:
        return escalate("답변 검사: " + ", ".join(problems), "check")
    out = s.call("run_action", {"action": "answer_inquiry", "target": iid, "params": {"answer": r["answer"]}})
    return Result(iid, inquiry["category"], "answered" if out["status"] == "completed" else "escalated",
                  responder.name, answer=r["answer"], docs=r.get("docs", []), status=out["status"])


def open_inquiries(client) -> list[dict]:
    return [o.properties for o in client.objects("Inquiry").where(status="접수").order_by("inquiry_id").all()]
