"""평가 질문 세트를 서버에 보내고 응답을 JSON 으로 저장한다.

evaluation/test_queries.csv 의 12건을 CSV 순서대로 POST /query 에 보낸다.
E01 은 P02 와 같은 session_id 로 P02 바로 다음에 보내고, 나머지는 세션 없이 보낸다.

실행: 서버를 켠 뒤 프로젝트 루트에서
    python evaluation/run_eval.py [--url http://127.0.0.1:8000/query] [--out evaluation/eval_results.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
QUERIES = EVAL_DIR / "test_queries.csv"
SESSION_IDS = {"P02", "E01"}  # 같은 세션으로 이어서 보내는 질문


def post(url: str, question: str, session_id: str | None) -> tuple[int, dict]:
    """질문 하나를 보내고 (HTTP 상태 코드, 응답 본문) 을 돌려준다."""
    body = json.dumps({"question": question, "session_id": session_id}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def summarize(item: dict) -> str:
    """응답 한 건을 한 줄 요약(도구 호출, 숫자 검증, 이동 경로 표현 점검, 해석 문장 점검)으로 만든다."""
    trace = item["response"].get("trace", [])
    tools = [t["도구"] for t in trace if "도구" in t]
    steps = {t.get("단계"): t for t in trace if "도구" not in t}
    numbers = steps.get("숫자 검증", {})
    routes = steps.get("이동 경로 표현 점검", {})
    interps = steps.get("해석 문장 점검", {})
    return (
        f"{item['id']} HTTP {item['status']} {item['sec']}s | 도구 {tools} | "
        f"숫자 {numbers.get('결과')} {numbers.get('처음 확인되지 않은 숫자', [])} | "
        f"경로 표현 {routes.get('결과')} {routes.get('처음 단정 표현', [])} | "
        f"해석 {interps.get('결과')} {interps.get('처음 어긋난 문장', [])}"
    )


def main() -> None:
    """평가 질문을 모두 보내고 결과를 저장한다."""
    parser = argparse.ArgumentParser(description="평가 질문 세트를 서버에 보낸다")
    parser.add_argument("--url", default="http://127.0.0.1:8000/query")
    parser.add_argument("--out", default=str(EVAL_DIR / "eval_results.json"))
    args = parser.parse_args()

    rows = list(csv.DictReader(QUERIES.open(encoding="utf-8")))
    session = "eval-" + uuid.uuid4().hex[:8]
    results = []
    for row in rows:
        session_id = session if row["id"] in SESSION_IDS else None
        start = time.time()
        status, response = post(args.url, row["input"], session_id)
        item = {
            "id": row["id"],
            "status": status,
            "sec": round(time.time() - start, 1),
            "response": response,
        }
        results.append(item)
        print(summarize(item), flush=True)

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {args.out}")


if __name__ == "__main__":
    main()
