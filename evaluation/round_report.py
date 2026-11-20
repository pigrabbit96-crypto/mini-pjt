"""채점된 라운드 결과로 라운드별 평가 리포트(evaluation/roundN_report.md)를 만든다.

입력
- evaluation/roundN_results.json: run_eval.py 의 응답 원본에 judge.py 가 Judge 판정과 지표를 덧붙인 파일
- evaluation/rounds.json: 라운드별 설명(날짜, 대상 모델, 코드 상태, 변경 사항, 사람 판정, 발견, 다음 개선)
- evaluation/test_queries.csv: 질문과 구분

리포트의 표와 숫자는 모두 결과 파일에서 계산하고, 설명 문장은 rounds.json 에서 가져온다.
그래서 리포트를 고칠 때는 리포트가 아니라 rounds.json 을 고친 뒤 다시 만든다.

실행: 프로젝트 루트에서
    python evaluation/round_report.py 5        # 라운드 5 리포트
    python evaluation/round_report.py all      # rounds.json 에 있는 모든 라운드
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
QUERIES = EVAL_DIR / "test_queries.csv"
ROUNDS = EVAL_DIR / "rounds.json"
METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
METRIC_LABEL = {
    "faithfulness": "Faithfulness",
    "answer_relevancy": "Answer relevancy",
    "context_precision": "Context precision",
    "context_recall": "Context recall",
}
CHECK_STEPS = ["숫자 검증", "이동 경로 표현 점검", "해석 문장 점검"]


def load_round(n: int) -> list[dict] | None:
    """라운드 n 의 채점된 결과를 읽는다. 파일이 없으면 None."""
    path = EVAL_DIR / f"round{n}_results.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def mean(values: list) -> float | None:
    """빈 값을 뺀 평균. 값이 없으면 None."""
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def fmt(value) -> str:
    """표에 넣을 숫자를 만든다. 빈 값은 '-'."""
    return "-" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def human_pass(verdict: str | None) -> bool | None:
    """사람 판정 문자열을 통과 여부로 바꾼다. '경계 통과'는 통과로 본다."""
    if not verdict:
        return None
    return not verdict.startswith("실패")


def rewrites(item: dict) -> list[str]:
    """응답 trace 에서 다시 쓰기를 요청한 점검 단계 이름을 모은다."""
    steps = {t.get("단계"): t for t in item["response"].get("trace", []) if "도구" not in t}
    return [name for name in CHECK_STEPS if steps.get(name, {}).get("재작성 요청")]


def withheld(item: dict) -> bool:
    """점검 단계 중 하나라도 '보류'로 끝났는지 본다."""
    return any(t.get("결과") == "보류" for t in item["response"].get("trace", []) if "도구" not in t)


def summarize(results: list[dict], humans: dict[str, str]) -> dict:
    """라운드 요약 값(통과 수, 일치율, 지표 평균, 다시 쓰기·보류 수)을 계산한다."""
    judged = [r for r in results if "judge" in r]
    agree = [human_pass(humans.get(r["id"])) == bool(r["judge"].get("통과"))
             for r in judged if human_pass(humans.get(r["id"])) is not None]
    return {
        "건수": len(results),
        "채점": len(judged),
        "Judge 통과": sum(bool(r["judge"].get("통과")) for r in judged),
        "사람 통과": sum(bool(human_pass(v)) for v in humans.values()) if humans else None,
        "일치": (sum(agree), len(agree)) if agree else None,
        "다시 쓰기": sum(bool(rewrites(r)) for r in results),
        "보류": sum(withheld(r) for r in results),
        **{k: mean([r.get("metrics", {}).get(k) for r in judged]) for k in METRICS},
    }


def section_info(meta: dict, judge_model: str) -> list[str]:
    """1장: 라운드 정보 표."""
    rows = [
        ("날짜", meta.get("날짜", "-")),
        ("평가 세트", meta.get("평가 세트", "-")),
        ("평가 대상 모델", f"`{meta.get('대상 모델', '-')}`"),
        ("채점 모델 (Judge)", f"`{judge_model}`"),
        ("코드 상태", meta.get("코드 상태", "-")),
        ("이전 라운드 이후 변경", "<br>".join(meta.get("변경", [])) or "-"),
    ]
    return ["## 1. 라운드 정보", "", "| 항목 | 내용 |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows], ""]


def section_summary(s: dict, prev: dict | None, n: int) -> list[str]:
    """2장: 요약과 이전 라운드 대비 변화."""
    lines = ["## 2. 요약", "", "| 항목 | 이번 라운드 |" + (f" 라운드 {n - 1} | 변화 |" if prev else ""),
             "|---|---|" + ("---|---|" if prev else "")]

    def row(label: str, now, before=None, diff: str = "") -> str:
        return f"| {label} | {now} |" + (f" {before} | {diff} |" if prev else "")

    lines.append(row("Judge 통과", f"{s['Judge 통과']}/{s['채점']}",
                     f"{prev['Judge 통과']}/{prev['채점']}" if prev else None,
                     f"{s['Judge 통과'] / s['채점'] - prev['Judge 통과'] / prev['채점']:+.0%}" if prev else ""))
    if s["사람 통과"] is not None:
        lines.append(row("사람 판정 통과 (경계 포함)", f"{s['사람 통과']}/{s['건수']}",
                         f"{prev['사람 통과']}/{prev['건수']}" if prev and prev["사람 통과"] is not None else "-", ""))
    else:
        lines.append(row("사람 판정 통과", "기록 없음",
                         f"{prev['사람 통과']}/{prev['건수']}" if prev and prev["사람 통과"] is not None else "-", ""))
    if s["일치"]:
        agree, total = s["일치"]
        lines.append(row("Judge·사람 판정 일치", f"{agree}/{total} ({agree / total:.0%})", "", ""))
    for key in METRICS:
        now, before = s[key], prev[key] if prev else None
        diff = f"{now - before:+.3f}" if prev and now is not None and before is not None else ""
        lines.append(row(f"{METRIC_LABEL[key]} 평균", fmt(now), fmt(before) if prev else None, diff))
    lines.append(row("다시 쓰기가 일어난 질문", s["다시 쓰기"], prev["다시 쓰기"] if prev else None, ""))
    lines.append(row("보류된 답변", s["보류"], prev["보류"] if prev else None, ""))
    lines += ["", "지표 정의와 적용 대상은 [judge.py](judge.py) 맨 위 설명에 있다. Context 지표는 데이터 질문(positive, edge)만 계산하고,"
              " faithfulness 는 근거(contexts)가 있고 사실 주장이 있는 답변만 계산한다. 빈 값은 평균에서 뺀다.", ""]
    return lines


def section_items(results: list[dict], humans: dict[str, str], categories: dict[str, str]) -> list[str]:
    """3장: 건별 결과 표."""
    lines = ["## 3. 건별 결과", "",
             "| ID | 구분 | 사람 판정 | Judge | Faith. | Relev. | Ctx prec. | Ctx recall | 다시 쓰기 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        m, j = r.get("metrics", {}), r.get("judge", {})
        judge = "-" if not j else ("통과" if j.get("통과") else "**불통과**")
        human = humans.get(r["id"], "-")
        mismatch = human != "-" and j and human_pass(human) != bool(j.get("통과"))
        lines.append(
            f"| {r['id']} | {categories.get(r['id'], '-')} | {human} | {judge}{' ⚠' if mismatch else ''} | "
            f"{fmt(m.get('faithfulness'))} | {fmt(m.get('answer_relevancy'))} | {fmt(m.get('context_precision'))} | "
            f"{fmt(m.get('context_recall'))} | {', '.join(rewrites(r)) or '-'} |"
        )
    lines += ["", "⚠ 표시는 Judge 판정이 사람 판정과 다른 건이다.", ""]
    return lines


def section_judge_details(results: list[dict], humans: dict[str, str]) -> list[str]:
    """4장: Judge 불통과이거나 사람 판정과 다른 건의 판정 근거."""
    lines = ["## 4. Judge 불통과·판정 불일치 상세", ""]
    picked = [r for r in results if r.get("judge") and (
        not r["judge"].get("통과") or (humans.get(r["id"]) and human_pass(humans[r["id"]]) != bool(r["judge"].get("통과")))
    )]
    if not picked:
        tail = "사람 판정과도 모두 같다." if humans else "이 라운드는 사람 판정 기록이 없어 일치 여부는 비교하지 않았다."
        return lines + [f"해당 없음. Judge 가 모든 건을 통과로 판정했다. {tail}", ""]
    for r in picked:
        j = r["judge"]
        lines += [
            f"- **{r['id']}** (사람: {humans.get(r['id'], '기록 없음')}, Judge: {'통과' if j.get('통과') else '불통과'})",
            f"  - 빠진 기대: {', '.join(j.get('빠진 기대') or []) or '없음'}",
            f"  - 위반한 금지: {', '.join(j.get('위반한 금지') or []) or '없음'}",
            f"  - Judge 이유: {j.get('이유', '-')}",
        ]
    return lines + [""]


def section_claims(results: list[dict]) -> list[str]:
    """5장: faithfulness 에서 근거 없음으로 판정된 주장."""
    lines = ["## 5. 근거로 확인되지 않은 주장 (faithfulness)", ""]
    found = False
    for r in results:
        claims = [c["내용"] for c in r.get("metrics", {}).get("세부", {}).get("주장", []) if not c.get("근거있음")]
        if claims:
            found = True
            lines.append(f"- **{r['id']}** (faithfulness {fmt(r['metrics']['faithfulness'])})")
            lines += [f"  - {c}" for c in claims]
    if not found:
        lines.append("해당 없음. 채점한 모든 주장이 근거로 확인됐다.")
    return lines + [""]


def section_notes(meta: dict) -> list[str]:
    """6·7장: 발견한 문제와 다음 라운드 개선 (rounds.json 의 설명)."""
    lines = ["## 6. 발견한 문제", ""]
    lines += [f"- {x}" for x in meta.get("발견", [])] or ["- (없음)"]
    lines += ["", "## 7. 다음 라운드 개선", ""]
    lines += [f"- {x}" for x in meta.get("다음 개선", [])] or ["- (없음)"]
    return lines + [""]


def build(n: int, rounds: dict) -> Path:
    """라운드 n 리포트를 만들어 저장하고 경로를 돌려준다."""
    meta = rounds[str(n)]
    results = load_round(n)
    if results is None:
        raise FileNotFoundError(f"round{n}_results.json 이 없다")
    categories = {r["id"]: r["category"] for r in csv.DictReader(QUERIES.open(encoding="utf-8"))}
    humans = meta.get("사람 판정", {})
    prev_results = load_round(n - 1)
    prev = summarize(prev_results, rounds.get(str(n - 1), {}).get("사람 판정", {})) if prev_results else None
    judge_model = next((r["judge"].get("모델") for r in results if r.get("judge")), "채점 전")

    lines = [f"# 라운드 {n} 평가 리포트 · 내 또래 인구 이동 Agent", "",
             f"원본 응답과 채점 결과: [round{n}_results.json](round{n}_results.json)", ""]
    lines += section_info(meta, judge_model)
    lines += section_summary(summarize(results, humans), prev, n)
    lines += section_items(results, humans, categories)
    lines += section_judge_details(results, humans)
    lines += section_claims(results)
    lines += section_notes(meta)

    path = EVAL_DIR / f"round{n}_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    """인자로 받은 라운드(또는 all)의 리포트를 만든다."""
    rounds = json.loads(ROUNDS.read_text(encoding="utf-8"))
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    numbers = sorted(int(k) for k in rounds) if target == "all" else [int(target)]
    for n in numbers:
        print("저장:", build(n, rounds))


if __name__ == "__main__":
    main()
