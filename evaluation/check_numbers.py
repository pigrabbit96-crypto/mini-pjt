"""평가 결과의 답변 숫자를 응답 trace 의 도구 결과와 다시 대조한다.

서버의 자동 숫자 검증과 별도로 확인하는 용도다. 서버 검증과 달리 1,000 미만 정수와
%·%p 값까지 보되, 연도·나이(세)·개수(개, 곳)처럼 인구 숫자가 아닌 것은 뺀다.
같은 세션의 앞 질문 도구 결과도 근거로 쓴다(E01 은 P02 의 결과를 이어받는다).
걸린 값은 사람이 다시 읽고 실제 불일치인지 판정한다.

실행: 프로젝트 루트에서
    python evaluation/check_numbers.py [evaluation/eval_results.json]
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
QUERIES = EVAL_DIR / "test_queries.csv"
SESSION_CHAIN = {"E01": "P02"}  # 앞 질문의 도구 결과를 근거로 함께 쓰는 질문

_NUMBER_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d,]*\d)")
_SKIP_AFTER_RE = re.compile(r"\s*(?:년|세|개|곳|위|단계|차|번|가지|월|일|명에|명 중|~|→|분|초)")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def tool_text(response: dict) -> str:
    """응답 trace 에서 도구 입력과 출력을 하나의 문자열로 모은다."""
    calls = [t for t in response.get("trace", []) if "도구" in t]
    return json.dumps([[t.get("입력"), t.get("출력")] for t in calls], ensure_ascii=False)


def unmatched_numbers(answer: str, evidence: str) -> list[str]:
    """답변 숫자 중 근거 문자열에서 찾을 수 없는 것을 돌려준다. 반올림한 값은 같은 숫자로 본다."""
    known = {abs(float(t.replace(",", ""))) for t in _NUMBER_RE.findall(evidence)}
    found = []
    for match in _NUMBER_RE.finditer(answer):
        token = match.group(1)
        if _SKIP_AFTER_RE.match(answer, match.end()) or _YEAR_RE.fullmatch(token):
            continue
        value = float(token.replace(",", ""))
        decimals = len(token.split(".")[1]) if "." in token else 0
        if not any(abs(round(k, decimals) - value) < 1e-9 for k in known):
            found.append(token)
    return list(dict.fromkeys(found))


def main() -> None:
    """결과 파일의 모든 답변을 대조하고 걸린 값을 출력한다."""
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else EVAL_DIR / "eval_results.json"
    results = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    questions = {r["id"]: r["input"] for r in csv.DictReader(QUERIES.open(encoding="utf-8"))}

    flagged = 0
    for qid, item in results.items():
        response = item["response"]
        evidence = questions[qid] + tool_text(response)
        if qid in SESSION_CHAIN:
            evidence += tool_text(results[SESSION_CHAIN[qid]]["response"])
        found = unmatched_numbers(response.get("answer", ""), evidence)
        flagged += bool(found)
        print(f"{qid}: 도구 결과에서 찾지 못한 숫자 {found}")
    print(f"\n걸린 값이 있는 답변 {flagged}건 (사람이 다시 읽고 판정한다)")


if __name__ == "__main__":
    main()
