"""19장. Ontology Registry · API · SDK

    uv run python 03-ontology-engine/examples/19_registry_api_sdk.py            # 모든 변경 롤백
    uv run python 03-ontology-engine/examples/19_registry_api_sdk.py --commit   # Registry 등록·활성화만 남김

1. Registry  — 정의 등록, 활성화 관문(AI·등록자 본인 거부), AI 가 만든 초안(현재 버전의 다음 부 버전) 비교, breaking 차단
2. API       — Registry 활성 버전을 REST API 로 서빙 (TestClient 로 호출하므로 포트가 필요 없다)
3. SDK       — 메타데이터로 타입 SDK 를 생성하고 같은 API 를 호출

업무 데이터(신고·자료·업무)의 변경은 --commit 과 상관없이 항상 롤백한다.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import inspect
import sys
import tempfile
import warnings
from contextlib import nullcontext
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

warnings.filterwarnings("ignore", message="Using `httpx` with")   # Starlette 의 향후 변경 예고 (실습과 무관)
from fastapi.testclient import TestClient  # noqa: E402

from minionto.api import Served, create_app, load_api_keys  # noqa: E402
from minionto.codegen import generate  # noqa: E402
from minionto.describe import public_metadata  # noqa: E402
from minionto.registry import Package, Registry, RegistryError  # noqa: E402

TAXOS = ENGINE / "ontologies" / "taxos" / "ontology.yaml"
KEYS = load_api_keys(ENGINE / "examples" / "dev_api_keys.yaml")
AS_OF = "2027-01-18"
REQ = "request_missing_documents"

AUTHOR = {"id": "A001", "role": "세무사", "type": "human"}          # 정의를 고친 사람
REVIEWER = {"id": "A005", "role": "세무사", "type": "human"}        # 검토·반영하는 사람
LLM = {"id": "onto-builder", "role": "AI비서", "type": "agent"}     # 정의 초안을 만드는 AI


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def draft_v2(base: Package) -> Package:
    """AI 가 업무 매뉴얼을 읽고 제안했다고 가정한 초안 (taxos v0.1.0 기준이면 v0.2.0)"""
    raw = yaml.safe_load(base.source_yaml)
    raw = copy.deepcopy(raw)
    major, minor, _ = base.onto.ontology.version.split(".")
    raw["ontology"]["version"] = f"{major}.{int(minor) + 1}.0"
    props = raw["object_types"]["Customer"]["properties"]
    props["email"] = {"type": "string", "display_name": "이메일"}                 # 속성 추가
    props.pop("phone")                                                            # 속성 삭제
    act = raw["actions"][REQ]
    act["parameters"]["channel"] = {"type": "enum", "values": ["문자", "메일"]}   # 필수 입력 추가
    act["permission"]["actor_types"] = ["human"]                                  # 권한 축소
    return Package.from_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), base.function_sources)


# ====================================================================== 1. Registry
def demo_registry(reg: Registry) -> Served:
    section("1. Ontology Registry — 등록 → 활성화 관문 → 버전 비교")
    base = Package.from_file(TAXOS)
    r = reg.register(base, actor=AUTHOR, notes="19장 실습")
    print(("등록" if r.created else "이미 등록됨") + f": {r.version}  (등록자 {AUTHOR['id']})")

    version = base.onto.ontology.version
    for who, actor in (("AI", LLM), ("등록자 본인", AUTHOR), ("검토자", REVIEWER)):
        try:
            reg.activate("taxos", version, actor=actor)
            print(f"  활성화 시도 — {who}: 성공 (DB 대조 검사 통과)")
        except RegistryError as e:
            print(f"  활성화 시도 — {who}: 거부 — {str(e).splitlines()[0]}")

    with reg.conn.transaction(force_rollback=True):        # AI 초안은 항상 되돌린다
        r2 = reg.register(draft_v2(base), actor=LLM, notes="LLM 초안: 업무 매뉴얼 기반")
        print(f"\nAI 가 초안 등록: {r2.version} — 현재 활성 v{r2.base} 대비 변경 {len(r2.changes)}건, "
              f"권장 버전 v{r2.suggested_version}")
        for c in r2.changes:
            print("  " + str(c))
        for allow in (False, True):
            try:
                reg.activate("taxos", r2.version.version, actor=REVIEWER, allow_breaking=allow)
                print("  검토자 활성화: 성공")
            except RegistryError as e:
                lines = str(e).splitlines()
                print(f"  검토자 활성화{' (allow_breaking)' if allow else ''}: 거부 — {lines[0]}")
                if allow:
                    print("   " + lines[1])                 # 첫 번째 사유

    print("\n등록 이력")
    for h in reg.history("taxos"):
        print(f"  v{h['version']:6s} {h['status']:8s} {h['checksum']}  등록 {h['registered_by']}  "
              f"반영 {h['activated_by'] or '-'}")
    return Served.from_registry(reg.get("taxos"))


# ====================================================================== 2. API
def demo_api(http: TestClient, engine) -> None:
    section("2. Ontology API — 활성 버전을 업무 용어로 연다")
    agent = {"Authorization": "Bearer dev-ai-agent"}
    tax = {"Authorization": "Bearer dev-tax-accountant"}
    staff = {"Authorization": "Bearer dev-staff-a003"}
    p = "/v1/ontologies/taxos"

    print("GET /v1/ontologies →", http.get("/v1/ontologies", headers=agent).json())
    print("GET /v1/ontologies (토큰 없음) →", http.get("/v1/ontologies").status_code)

    meta = http.get(p, headers=agent).json()
    print(f"\nGET {p} — AI 에이전트가 읽는 'Action 목록'")
    for name, a in meta["actions"].items():
        appr = f", 승인 {'/'.join(a['approval']['approver_roles'])}" if a["approval"] else ""
        print(f"  {name:28s} 대상 {a['target']:10s} 권한 {'/'.join(a['permission']['roles'])}{appr}")

    body = {"where": [{"field": "status", "op": "eq", "value": "자료수집중"},
                      {"field": "customer.risk_level", "op": "eq", "value": "높음"}],
            "order_by": ["due_date"], "select": ["return_id", "tax_kind", "due_date", "customer.accountant_id"],
            "limit": 3}
    r = http.post(f"{p}/objects/TaxReturn/search", json=body, headers=agent).json()
    print(f"\nPOST .../objects/TaxReturn/search (자료수집중 · 고위험) → 전체 {r['total']}건")
    for row in r["data"]:
        print("  ", row)

    agg = http.post(f"{p}/objects/Task/aggregate", headers=agent,
                    json={"where": [{"field": "status", "op": "in", "value": ["대기", "진행중"]}],
                          "count_by": ["assignee.accountant_id"]}).json()
    print("\nPOST .../objects/Task/aggregate (담당자별 미완료 업무) →",
          ", ".join(f"{g['assignee.accountant_id']}:{g['count']}" for g in agg["groups"]))

    cust = r["data"][0]["return_id"]
    customer = http.get(f"{p}/objects/TaxReturn/{cust}/links/customer", headers=agent).json()["data"]["pk"]
    risk = http.post(f"{p}/functions/filing_delay_risk", headers=agent, json={"args": {"customer": customer}})
    print(f"\nPOST .../functions/filing_delay_risk ({customer}) →", risk.json()["result"])

    cands = http.get(f"{p}/actions/{REQ}/candidates?limit=50", headers=agent).json()
    print(f"\nGET .../actions/{REQ}/candidates (AI비서) → {cands['total']}건")
    normal = next(c["pk"] for c in cands["data"]
                  if (c_ := engine.get("TaxReturn", c["pk"]).customer).risk_level != "높음"
                  and c_.accountant_id != "A003")
    risky = next(c["pk"] for c in cands["data"] if engine.get("TaxReturn", c["pk"]).customer.risk_level == "높음")

    def show(label, resp):
        b = resp.json()
        print(f"  {label:34s} HTTP {resp.status_code} {b['status']}"
              + (f" — {b['messages'][0]}" if b["messages"] else ""))
        return b

    print("\nPOST .../actions/{action}/apply")
    show(f"실무자 A003 → {normal}", http.post(f"{p}/actions/{REQ}/apply", headers=staff,
                                             json={"target": normal, "actor": {"role": "세무사"}}))
    show(f"AI비서 dry-run → {normal}", http.post(f"{p}/actions/{REQ}/apply", headers=agent,
                                               json={"target": normal, "dry_run": True}))
    show(f"AI비서 → {normal}", http.post(f"{p}/actions/{REQ}/apply", headers=agent, json={"target": normal}))
    show(f"AI비서 다시 → {normal}", http.post(f"{p}/actions/{REQ}/apply", headers=agent, json={"target": normal}))
    pending = show(f"AI비서 → {risky} (고위험)", http.post(f"{p}/actions/{REQ}/apply", headers=agent,
                                                         json={"target": risky}))
    mine = http.get(f"{p}/approvals", headers=tax).json()["data"]
    print(f"  GET .../approvals (세무사) → {len(mine)}건 대기")
    show("세무사 승인 → 실행", http.post(f"{p}/approvals/{pending['approval_id']}/decide", headers=tax,
                                    json={"approve": True, "comment": "확인"}))


# ====================================================================== 3. SDK
def demo_sdk(http: TestClient, served: Served) -> None:
    section("3. SDK Generator — 메타데이터 → 타입이 있는 Python 클라이언트")
    code = generate(public_metadata(served.onto, version=served.version, checksum=served.checksum))
    out = Path(tempfile.mkdtemp()) / "taxos_sdk.py"
    out.write_text(code, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("taxos_sdk", out)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["taxos_sdk"] = mod
    spec.loader.exec_module(mod)
    print(f"생성: taxos_sdk.py ({len(code.splitlines())}줄) — dataclass {len(mod.TaxosClient.models)}개")
    print("  def request_missing_documents" + str(inspect.signature(mod.TaxosClient.request_missing_documents)))
    print("  def assign_accountant" + str(inspect.signature(mod.TaxosClient.assign_accountant)))

    sdk = mod.TaxosClient("http://testserver", token="dev-ai-agent", http=http)
    print(f"\nsdk.check_version() → 서버 v{sdk.check_version()} (SDK v{sdk.version})")
    returns = sdk.TaxReturn.where(status="자료수집중").order_by("due_date").limit(2).all()
    for r in returns:
        print(f"  {type(r).__name__}(return_id={r.return_id!r}, due_date={r.due_date!r})")
    customer = sdk.TaxReturn.where(return_id=returns[0].return_id).pivot("customer").first()
    print(f"  .pivot('customer') → {type(customer).__name__}(customer_id={customer.customer_id!r}, "
          f"risk_level={customer.risk_level!r})")
    print("  sdk.has_capacity('A002') →", sdk.has_capacity("A002"))
    target = sdk.candidates(REQ, limit=1)[0].pk
    print(sdk.request_missing_documents(target, note="SDK 에서 요청", dry_run=True))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Registry 등록·활성화를 남긴다")
    args = ap.parse_args()

    with Registry() as reg:
        with reg.conn.transaction(force_rollback=not args.commit):
            served = demo_registry(reg)

    engine = served.client(today=AS_OF)
    with engine, engine.conn.transaction(force_rollback=True):   # 업무 데이터 변경은 항상 롤백
        app = create_app([served], api_keys=KEYS, today=AS_OF, connect=lambda s: nullcontext(engine))
        with TestClient(app) as http:
            demo_api(http, engine)
            demo_sdk(http, served)
    print("\n(업무 데이터 변경은 롤백했습니다" + (", Registry 등록·활성화는 반영" if args.commit else "") + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
