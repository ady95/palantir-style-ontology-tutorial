"""AI Native Tax OS 실습용 합성 데이터 생성기

세무 사무소의 고객·신고·필수 자료·상담 이력·후속 업무·위험 이벤트를 가상으로 만듭니다.
모든 이름·번호·연락처는 무작위로 조합한 가상 값이며 실제 인물·사업자와 관계가 없습니다.

- 표준 라이브러리만 사용 (Python 3.10+)
- 시드 고정: 같은 옵션이면 항상 같은 데이터가 생성됩니다.

사용법:
    python scripts/generate_taxos_data.py                       # datasets/taxos/data/*.csv 생성
    python scripts/generate_taxos_data.py --customers 500 --seed 7
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "datasets" / "taxos" / "data"

# 기준일: 2026년 2기 부가가치세 확정신고 기한(2027-01-25) 일주일 전
AS_OF = date(2027, 1, 18)
CURRENT_DUE = date(2027, 1, 25)
VAT = "부가가치세 확정신고"

# (기간, 기한) — 일반과세자·법인은 반기, 간이과세자는 연 1회
PAST_PERIODS_HALF = [("2025-2기", date(2026, 1, 26)), ("2026-1기", date(2026, 7, 27))]
PAST_PERIODS_YEAR = [("2025년", date(2026, 1, 26))]
CURRENT_PERIOD_HALF = "2026-2기"
CURRENT_PERIOD_YEAR = "2026년"

SURNAMES = list("김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부가복태목형피두감음빈동온호범좌팽승간상시갈단견당화창")[:60]
GIVEN = list("민서지현준우하윤도영수진예은성호재희정아연주태상경혜원나린채율건시유승동규선미보람철환석")

COMPANY_PREFIX = ["한빛", "푸른", "새봄", "다온", "가람", "늘해", "온누리", "해든", "미래", "누리",
                  "청솔", "별빛", "하늘", "바른", "우리", "도담", "소담", "라온", "나래", "다솜",
                  "힘찬", "모아", "마루", "이음", "솔빛", "단비", "초록", "은하", "희망", "참좋은"]

# 업종: (업종명, 상호 접미어, 법인 가능 여부, 간이과세 가능 여부)
INDUSTRIES = [
    ("음식점업", ["식당", "분식", "국밥", "반점"], False, True),
    ("카페", ["커피", "카페", "베이커리"], False, True),
    ("미용업", ["헤어", "뷰티"], False, True),
    ("의류 소매업", ["의류", "패션", "옷가게"], True, True),
    ("온라인 쇼핑몰", ["몰", "스토어", "마켓"], True, True),
    ("소프트웨어 개발", ["소프트", "테크", "랩스"], True, False),
    ("디자인 서비스", ["디자인", "스튜디오"], True, True),
    ("학원", ["학원", "아카데미"], True, False),
    ("부품 제조업", ["정밀", "산업", "공업"], True, False),
    ("건설업", ["건설", "건축", "종합건설"], True, False),
    ("식자재 도매업", ["유통", "상사", "푸드"], True, False),
    ("화물 운송업", ["물류", "운수", "로지스"], True, True),
    ("부동산 임대업", ["빌딩", "개발", "산업개발"], True, True),
]

# (코드, 자료명, 개인 일반, 개인 간이, 법인)
DOCUMENT_TYPES = [
    ("D01", "매출 세금계산서 내역", True, True, True),
    ("D02", "매입 세금계산서 내역", True, True, True),
    ("D03", "신용카드·현금영수증 매출 내역", True, True, True),
    ("D04", "사업용 신용카드 사용내역", True, False, True),
    ("D05", "법인 통장 거래내역", False, False, True),
]

CONSULT_TOPICS = {
    "신고 일정 문의": "{period} 부가가치세 신고 기한과 준비 자료를 문의함. 필수 자료 목록을 안내함.",
    "자료 제출 지연": "{doc} 준비가 늦어진다고 알림. {n}일 안에 제출하기로 함.",
    "세금계산서 발행 문의": "거래처에 발행할 세금계산서 작성 방법을 문의함. 발행 절차를 안내함.",
    "경비 처리 문의": "사업용 경비로 처리할 수 있는 항목을 문의함. 증빙 보관 방법을 안내함.",
    "수수료 문의": "기장 수수료와 신고 대행 수수료를 문의함. 계약 조건을 다시 안내함.",
    "가산세 문의": "지난 신고의 가산세 부과 사유를 문의함. 기한 준수 필요성을 설명함.",
    "휴업 신고 문의": "매출 감소로 휴업을 고려 중이라고 함. 휴업 신고 절차와 신고 의무를 안내함.",
}
CHANNELS = ["전화", "카카오톡", "이메일", "방문"]


def fake_person(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + rng.choice(GIVEN) + rng.choice(GIVEN)


def build_accountants(rng: random.Random) -> list[dict]:
    rows = []
    seq = 1
    for team in ["세무1팀", "세무2팀"]:
        for role, n in [("세무사", 1), ("실무자", 3)]:
            for _ in range(n):
                aid = f"A{seq:03d}"
                rows.append({
                    "accountant_id": aid,
                    "name": fake_person(rng),
                    "role": role,
                    "team": team,
                    "max_workload": 60 if role == "세무사" else 40,
                    "can_approve": "true" if role == "세무사" else "false",
                    "email": f"{aid.lower()}@example.com",
                })
                seq += 1
    return rows


def build_customers(rng: random.Random, n: int, accountants: list[dict]) -> list[dict]:
    staff = [a for a in accountants if a["role"] == "실무자"]
    # 첫 번째 실무자에게 고객을 몰아 업무량 초과 사례를 만든다
    weights = [0.30, 0.20, 0.15, 0.15, 0.10, 0.10][: len(staff)]
    used_names: set[str] = set()
    rows = []
    for i in range(1, n + 1):
        industry, suffixes, corp_ok, simple_ok = rng.choice(INDUSTRIES)
        business_type = "법인" if corp_ok and rng.random() < 0.40 else "개인"
        vat_type = "간이과세자" if business_type == "개인" and simple_ok and rng.random() < 0.35 else "일반과세자"

        name = rng.choice(COMPANY_PREFIX) + rng.choice(suffixes)
        if business_type == "법인":
            name = f"(주){name}"
        base, k = name, 2
        while name in used_names:
            name, k = f"{base}{k}", k + 1
        used_names.add(name)

        risk = rng.choices(["낮음", "보통", "높음"], weights=[50, 35, 15])[0]
        status = rng.choices(["영업", "휴업", "폐업"], weights=[94, 4, 2])[0]
        rows.append({
            "customer_id": f"C{i:04d}",
            "company_name": name,
            "representative": fake_person(rng),
            "business_reg_no": f"999-{rng.randint(10, 99)}-{i:05d}",
            "business_type": business_type,
            "vat_type": vat_type,
            "industry": industry,
            "status": status,
            "risk_level": risk,
            "accountant_id": rng.choices(staff, weights=weights)[0]["accountant_id"],
            "phone": f"010-0000-{i:04d}",
            "contracted_at": (date(2022, 1, 1) + timedelta(days=rng.randint(0, 1200))).isoformat(),
        })
    return rows


def diligence(rng: random.Random, risk: str) -> float:
    """고객의 성실도(0~1). 높을수록 자료를 제때 낸다. 생성 내부에서만 쓰고 저장하지 않는다."""
    lo, hi = {"낮음": (0.75, 1.0), "보통": (0.45, 0.8), "높음": (0.1, 0.5)}[risk]
    return rng.uniform(lo, hi)


def applicable_docs(c: dict) -> list[str]:
    col = 4 if c["business_type"] == "법인" else (3 if c["vat_type"] == "간이과세자" else 2)
    return [d[0] for d in DOCUMENT_TYPES if d[col]]


def approver_for(c: dict, accountants: list[dict]) -> str:
    team = next(a["team"] for a in accountants if a["accountant_id"] == c["accountant_id"])
    return next(a["accountant_id"] for a in accountants if a["team"] == team and a["can_approve"] == "true")


def generate(n_customers: int, seed: int, n_unregistered: int) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    accountants = build_accountants(rng)
    customers = build_customers(rng, n_customers, accountants)
    doc_names = {d[0]: d[1] for d in DOCUMENT_TYPES}

    returns, submissions, consults, tasks, events = [], [], [], [], []
    ids = Counter()

    def nid(prefix: str, width: int) -> str:
        ids[prefix] += 1
        return f"{prefix}{ids[prefix]:0{width}d}"

    # 자료 현황이 아직 등록되지 않은 고객 (Open World 문제 실습용)
    active = [c for c in customers if c["status"] == "영업"]
    unregistered = {c["customer_id"] for c in rng.sample(active, min(n_unregistered, len(active)))}

    for c in customers:
        d = diligence(rng, c["risk_level"])
        approver = approver_for(c, accountants)
        is_simple = c["vat_type"] == "간이과세자"
        late_history = False

        # 1) 과거 신고
        for period, due in (PAST_PERIODS_YEAR if is_simple else PAST_PERIODS_HALF):
            late = rng.random() < (1 - d) * 0.45
            filed = due + timedelta(days=rng.randint(3, 25)) if late else due - timedelta(days=rng.randint(1, 12))
            rid = nid("R", 5)
            returns.append({"return_id": rid, "customer_id": c["customer_id"], "tax_kind": VAT, "period": period,
                            "due_date": due.isoformat(), "status": "기한후신고" if late else "신고완료",
                            "filed_at": filed.isoformat()})
            tasks.append({"task_id": nid("T", 5), "customer_id": c["customer_id"], "return_id": rid,
                          "task_type": "신고서검토", "assignee_id": approver, "status": "완료",
                          "due_date": due.isoformat(), "created_by": "human",
                          "created_at": datetime.combine(due - timedelta(days=10), datetime.min.time()).replace(hour=9).isoformat(sep=" "),
                          "note": ""})
            if late:
                late_history = True
                events.append({"event_id": nid("E", 4), "customer_id": c["customer_id"], "event_type": "기한후신고",
                               "occurred_at": filed.isoformat(), "note": f"{period} 부가가치세 기한 후 신고"})
                if rng.random() < 0.6:
                    events.append({"event_id": nid("E", 4), "customer_id": c["customer_id"], "event_type": "가산세부과",
                                   "occurred_at": (filed + timedelta(days=rng.randint(20, 60))).isoformat(),
                                   "note": "무신고·납부지연 가산세"})
            elif rng.random() < (1 - d) * 0.3:
                events.append({"event_id": nid("E", 4), "customer_id": c["customer_id"], "event_type": "자료지연제출",
                               "occurred_at": (due - timedelta(days=rng.randint(1, 3))).isoformat(),
                               "note": f"{period} 자료를 기한 직전에 제출"})

        if c["risk_level"] == "높음" and rng.random() < 0.3:
            events.append({"event_id": nid("E", 4), "customer_id": c["customer_id"], "event_type": "연락두절",
                           "occurred_at": (AS_OF - timedelta(days=rng.randint(10, 90))).isoformat(),
                           "note": "자료 요청 후 2주 이상 연락되지 않음"})

        # 2) 현재 신고 (폐업 고객 제외)
        if c["status"] == "폐업":
            pass
        else:
            rid = nid("R", 5)
            period = CURRENT_PERIOD_YEAR if is_simple else CURRENT_PERIOD_HALF
            ret = {"return_id": rid, "customer_id": c["customer_id"], "tax_kind": VAT, "period": period,
                   "due_date": CURRENT_DUE.isoformat(), "status": "자료수집중", "filed_at": ""}
            returns.append(ret)

            if c["customer_id"] not in unregistered:
                statuses = []
                requested_dates = []
                for code in applicable_docs(c):
                    r = rng.random()
                    req = sub = ""
                    if c["status"] == "휴업":
                        st = rng.choice(["미요청", "해당없음"])
                    elif code == "D04" and rng.random() < 0.08:
                        st = "해당없음"
                    elif r < 0.35 + d * 0.65:
                        st = "제출완료"
                        s_date = AS_OF - timedelta(days=rng.randint(1, 30))
                        sub = s_date.isoformat()
                        req = (s_date - timedelta(days=rng.randint(1, 10))).isoformat()
                    elif r < 0.92:
                        st = "요청됨"
                        r_date = AS_OF - timedelta(days=rng.randint(2, 25))
                        req = r_date.isoformat()
                        requested_dates.append(r_date)
                    else:
                        st = "미요청"
                    statuses.append(st)
                    submissions.append({"submission_id": nid("S", 6), "return_id": rid, "doc_type_code": code,
                                        "status": st, "requested_at": req, "submitted_at": sub})

                if all(s in ("제출완료", "해당없음") for s in statuses) and "제출완료" in statuses:
                    if d > 0.9 and rng.random() < 0.35:
                        ret["status"] = "신고완료"
                        ret["filed_at"] = (AS_OF - timedelta(days=rng.randint(0, 5))).isoformat()
                    else:
                        ret["status"] = "검토중"
                        tasks.append({"task_id": nid("T", 5), "customer_id": c["customer_id"], "return_id": rid,
                                      "task_type": "신고서검토", "assignee_id": approver,
                                      "status": rng.choice(["대기", "진행중"]), "due_date": (CURRENT_DUE - timedelta(days=2)).isoformat(),
                                      "created_by": "human",
                                      "created_at": datetime.combine(AS_OF - timedelta(days=rng.randint(0, 3)), datetime.min.time()).replace(hour=10).isoformat(sep=" "),
                                      "note": ""})

                if requested_dates:
                    pending = [doc_names[s["doc_type_code"]] for s in submissions
                               if s["return_id"] == rid and s["status"] == "요청됨"]
                    tasks.append({"task_id": nid("T", 5), "customer_id": c["customer_id"], "return_id": rid,
                                  "task_type": "자료요청", "assignee_id": c["accountant_id"], "status": "진행중",
                                  "due_date": (CURRENT_DUE - timedelta(days=5)).isoformat(), "created_by": "human",
                                  "created_at": datetime.combine(min(requested_dates), datetime.min.time()).replace(hour=14).isoformat(sep=" "),
                                  "note": "요청 자료: " + ", ".join(pending)})

        # 3) 상담 이력
        n_consult = rng.randint(0, 3) + (2 if c["risk_level"] == "높음" else 0)
        topics = ["신고 일정 문의", "자료 제출 지연", "세금계산서 발행 문의", "경비 처리 문의", "수수료 문의"]
        if late_history:
            topics.append("가산세 문의")
        for k in range(n_consult):
            topic = "휴업 신고 문의" if c["status"] == "휴업" and k == 0 else rng.choice(topics)
            at = datetime.combine(AS_OF - timedelta(days=rng.randint(1, 365)), datetime.min.time()) \
                + timedelta(hours=rng.randint(9, 17), minutes=rng.choice([0, 10, 20, 30, 40, 50]))
            summary = CONSULT_TOPICS[topic].format(
                period=CURRENT_PERIOD_YEAR if is_simple else CURRENT_PERIOD_HALF,
                doc=doc_names[rng.choice(applicable_docs(c))], n=rng.randint(3, 10))
            handler = c["accountant_id"] if rng.random() < 0.85 else approver
            consults.append({"consultation_id": nid("H", 5), "customer_id": c["customer_id"], "accountant_id": handler,
                             "channel": rng.choice(CHANNELS), "consulted_at": at.isoformat(sep=" "),
                             "topic": topic, "summary": summary})

    consults.sort(key=lambda r: r["consulted_at"])
    document_types = [{"doc_type_code": d[0], "doc_name": d[1],
                       "for_general": str(d[2]).lower(), "for_simplified": str(d[3]).lower(),
                       "for_corporation": str(d[4]).lower()} for d in DOCUMENT_TYPES]

    return {
        "accountants": accountants,
        "customers": customers,
        "tax_returns": returns,
        "document_types": document_types,
        "document_submissions": submissions,
        "consultations": consults,
        "tasks": tasks,
        "risk_events": events,
    }


def write_csv(out_dir: Path, tables: dict[str, list[dict]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with open(out_dir / f"{name}.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 출력
    p = argparse.ArgumentParser(description="AI Native Tax OS 합성 데이터 생성")
    p.add_argument("--customers", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--unregistered", type=int, default=6, help="자료 현황 미등록 고객 수")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    tables = generate(args.customers, args.seed, args.unregistered)
    write_csv(args.out, tables)

    print(f"기준일 {AS_OF}, 신고 기한 {CURRENT_DUE}, 출력: {args.out}")
    for name, rows in tables.items():
        print(f"  {name:22s} {len(rows):>6,d}")


if __name__ == "__main__":
    main()
