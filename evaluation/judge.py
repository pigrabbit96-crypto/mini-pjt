"""평가 결과를 LLM-as-Judge 와 RAGAS 방식 지표로 채점한다.

ragas 라이브러리를 쓰지 않고, RAGAS 의 지표 정의를 Bedrock Claude 판정과 Titan 임베딩으로 직접 구현한다.
채점 모델은 평가 대상(Sonnet 4.6)과 다른 모델을 써서 자기 평가 편향을 줄인다(기본 Haiku 4.5).
라운드끼리 비교할 수 있도록 모든 라운드를 같은 채점 모델로 채점한다. 바꾸려면 .env 에 JUDGE_MODEL_ID 를 둔다.

건마다 계산하는 것
- Judge 통과: 질문·기대 항목(expected_traits)·금지 항목(forbidden)·비고(note)를 주고, 기대 항목을 모두 채우고
  금지 항목이 없으면 통과로 판정한다. '가점'이라고 적힌 내용은 통과 조건이 아니다.
- faithfulness: 답변을 사실 주장으로 나누고, 근거(contexts)로 뒷받침되는 주장의 비율. 사실 주장이 없으면 빈 값.
- answer relevancy: 답변만 보고 그 답변이 답하는 질문을 3개 만든 뒤, 원래 질문과의 임베딩 코사인 유사도 평균.
  답을 회피하거나 얼버무린 답변(noncommittal)은 0. 범위 밖 질문을 거절한 답변도 RAGAS 정의대로 0에 가깝게 나온다.
- context precision: 근거를 contexts 순서대로 보며 기준 답(expected_traits)에 쓸모 있었는지 판정하고 평균 정밀도를 낸다.
- context recall: 기준 답의 사실 항목 중 근거로 뒷받침되는 비율. 답변 형식에 관한 항목('~을 밝힘' 등)은 뺀다.
- context 지표 두 개는 데이터를 조회해 답해야 하는 질문(positive, edge)에만 계산한다.

근거(contexts)는 응답의 contexts(도구 결과와 설명 문서 섹션)다. E01 처럼 앞 질문을 이어받는 질문은 앞 질문의 contexts 도 붙인다.

실행: 프로젝트 루트에서
    python evaluation/judge.py evaluation/round5_results.json [--workers 2] [--force]
결과는 같은 파일의 건별 항목에 "judge", "metrics" 로 덧붙인다. 한 건씩 끝날 때마다 저장하고,
이미 채점한 건은 건너뛰므로 호출 한도로 멈춰도 다시 실행하면 이어서 채점한다(--force 면 모두 다시 채점).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from botocore.config import Config
from dotenv import load_dotenv

EVAL_DIR = Path(__file__).resolve().parent
load_dotenv(EVAL_DIR.parent / ".env")

from langchain_aws import BedrockEmbeddings, ChatBedrockConverse  # noqa: E402

QUERIES = EVAL_DIR / "test_queries.csv"
JUDGE_MODEL_ID = os.getenv("JUDGE_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
N_QUESTIONS = 3
DATA_CATEGORIES = {"positive", "edge"}
SESSION_CHAIN = {"E01": "P02"}  # 앞 질문의 contexts 를 함께 근거로 쓰는 질문
MAX_CONTEXT_CHARS = 6000  # 근거 하나가 너무 길면 앞부분만 쓴다
# 분당 호출 한도(Too many requests)에 걸리면 기다렸다가 다시 시도한다.
RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

JUDGE_PROMPT = """너는 인구 통계 질의응답 에이전트의 답변을 채점하는 평가자다.

[질문]
{question}

[기대 항목] (세미콜론으로 구분. 모두 충족해야 한다)
{expected}

[금지 항목] (세미콜론으로 구분. 하나라도 해당하면 불통과)
{forbidden}

[비고] ('가점'이라고 적힌 것은 통과 조건이 아니다. 비고에 허용 조건이 있으면 따른다)
{note}

[답변]
{answer}

판정 규칙
- 기대 항목을 하나씩 보고, 답변이 그 내용을 담고 있으면 충족이다. 숫자는 표기(쉼표, 부호, '%' 위치)만 달라도 값이 같으면 충족이다.
- 금지 항목에 해당하는 내용이 답변에 있으면 위반이다.
- 모든 기대 항목을 충족하고 위반이 없으면 통과다.

아래 JSON 하나만 출력한다.
{{"충족한 기대": ["..."], "빠진 기대": ["..."], "위반한 금지": ["..."], "통과": true, "이유": "한두 문장"}}"""

FAITHFULNESS_PROMPT = """답변의 사실 주장이 근거로 뒷받침되는지 판정한다.

[근거]
{contexts}

[답변]
{answer}

할 일
1. 답변을 짧고 독립된 사실 주장으로 나눈다. 인구수·비율·순위·변화·정의·데이터 범위·한계 설명처럼 참·거짓을 따질 수 있는 문장만 주장이다.
   인사, 추가 질문 권유, 예시 질문 목록, 기준 시점 문구만 있는 문장은 주장이 아니다.
2. 각 주장이 근거에서 직접 확인되거나 근거로부터 바로 따라 나오면 true, 아니면 false 다.
   데이터가 없다는 안내(예: '시군구 데이터는 없다')는 근거에 그런 내용이 있을 때만 true 다.

아래 JSON 하나만 출력한다. 주장이 없으면 빈 목록이다.
{{"주장": [{{"내용": "...", "근거있음": true}}]}}"""

RELEVANCY_PROMPT = """아래 답변만 보고, 이 답변이 답하고 있는 질문을 {n}개 만든다. 질문은 한국어로, 서로 다르게 쓴다.
답변이 질문에 답하지 않고 회피하거나 얼버무리면(예: '모르겠다', '답할 수 없다'만 있음) noncommittal 을 true 로 한다.

[답변]
{answer}

아래 JSON 하나만 출력한다.
{{"질문": ["...", "..."], "noncommittal": false}}"""

PRECISION_PROMPT = """질문과 기준 답이 있을 때, 번호 붙은 근거 각각이 기준 답에 이르는 데 쓸모 있었는지 판정한다.

[질문]
{question}

[기준 답]
{reference}

[근거]
{contexts}

아래 JSON 하나만 출력한다. 목록 길이는 근거 개수와 같고, 순서도 근거 번호 순서다.
{{"쓸모": [true, false]}}"""

RECALL_PROMPT = """기준 답의 각 항목이 근거로 뒷받침되는지 판정한다.

[기준 답 항목]
{statements}

[근거]
{contexts}

할 일
- 항목마다 '사실 항목'인지 먼저 정한다. 인구수·비율·순위·변화·데이터 범위처럼 근거로 확인할 수 있는 내용이면 사실 항목이다.
  '기준 시점 명시', '~라고 표현', '가정을 밝힘'처럼 답변 형식에 관한 항목은 사실 항목이 아니다.
- 사실 항목이면 근거로 뒷받침되는지 true/false 로 판정한다.

아래 JSON 하나만 출력한다. 목록 길이와 순서는 기준 답 항목과 같다.
{{"항목": [{{"사실": true, "근거있음": true}}]}}"""


def _chat() -> ChatBedrockConverse:
    """채점용 모델을 만든다. 같은 입력에 같은 판정이 나오도록 temperature 0 을 쓴다."""
    return ChatBedrockConverse(
        model=JUDGE_MODEL_ID,
        region_name=os.getenv("AWS_DEFAULT_REGION"),
        temperature=0,
        max_tokens=4000,
        config=RETRY_CONFIG,
    )


def ask_json(model: ChatBedrockConverse, prompt: str) -> dict:
    """모델에 물어 답의 첫 JSON 객체를 돌려준다. 읽지 못하면 한 번 더 묻는다."""
    for _ in range(2):
        text = model.invoke(prompt).text
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError("채점 모델의 답에서 JSON 을 읽지 못했다")


def _number_contexts(contexts: list[str]) -> str:
    """근거 목록에 번호를 붙이고 너무 긴 근거는 잘라 하나의 글로 만든다."""
    return "\n\n".join(f"[근거 {i}]\n{c[:MAX_CONTEXT_CHARS]}" for i, c in enumerate(contexts, 1))


def _cosine(a: list[float], b: list[float]) -> float:
    """두 벡터의 코사인 유사도."""
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def judge_pass(model, row: dict, answer: str) -> dict:
    """기대 항목·금지 항목으로 통과 여부를 판정한다."""
    return ask_json(model, JUDGE_PROMPT.format(
        question=row["input"], expected=row["expected_traits"],
        forbidden=row["forbidden"] or "(없음)", note=row["note"] or "(없음)", answer=answer,
    ))


def faithfulness(model, answer: str, contexts: list[str]) -> tuple[float | None, list[dict]]:
    """근거로 뒷받침되는 주장의 비율과 주장 목록을 돌려준다. 근거나 주장이 없으면 빈 값."""
    if not contexts:
        return None, []
    claims = ask_json(model, FAITHFULNESS_PROMPT.format(contexts=_number_contexts(contexts), answer=answer)).get("주장", [])
    if not claims:
        return None, []
    return round(sum(bool(c.get("근거있음")) for c in claims) / len(claims), 3), claims


def answer_relevancy(model, embedder: BedrockEmbeddings, question: str, answer: str) -> tuple[float, dict]:
    """답변에서 만든 질문들과 원래 질문의 코사인 유사도 평균. 회피한 답변은 0."""
    result = ask_json(model, RELEVANCY_PROMPT.format(n=N_QUESTIONS, answer=answer))
    generated = [q for q in result.get("질문", []) if q][:N_QUESTIONS]
    if result.get("noncommittal") or not generated:
        return 0.0, result
    vectors = embedder.embed_documents([question, *generated])
    score = sum(_cosine(vectors[0], v) for v in vectors[1:]) / len(generated)
    return round(score, 3), result


def context_precision(model, question: str, reference: str, contexts: list[str]) -> tuple[float | None, list[bool]]:
    """근거 순서대로 쓸모 여부를 판정해 평균 정밀도를 낸다. 쓸모 있는 근거가 없으면 0."""
    if not contexts:
        return None, []
    useful = [bool(v) for v in ask_json(model, PRECISION_PROMPT.format(
        question=question, reference=reference, contexts=_number_contexts(contexts),
    )).get("쓸모", [])][: len(contexts)]
    if not any(useful):
        return 0.0, useful
    hits, total = 0, 0.0
    for k, v in enumerate(useful, 1):
        if v:
            hits += 1
            total += hits / k
    return round(total / hits, 3), useful


def context_recall(model, reference: str, contexts: list[str]) -> tuple[float | None, list[dict]]:
    """기준 답의 사실 항목 중 근거로 뒷받침되는 비율. 사실 항목이 없으면 빈 값."""
    statements = [s.strip() for s in reference.split(";") if s.strip()]
    if not contexts or not statements:
        return None, []
    items = ask_json(model, RECALL_PROMPT.format(
        statements="\n".join(f"{i}. {s}" for i, s in enumerate(statements, 1)), contexts=_number_contexts(contexts),
    )).get("항목", [])
    facts = [it for it in items[: len(statements)] if it.get("사실")]
    detail = [{"항목": s, **it} for s, it in zip(statements, items)]
    if not facts:
        return None, detail
    return round(sum(bool(it.get("근거있음")) for it in facts) / len(facts), 3), detail


def score_item(item: dict, row: dict, contexts: list[str], model, embedder) -> dict:
    """한 건을 채점해 judge 와 metrics 를 돌려준다."""
    answer = item["response"].get("answer", "")
    judge = judge_pass(model, row, answer)
    faith, claims = faithfulness(model, answer, contexts)
    relevancy, generated = answer_relevancy(model, embedder, row["input"], answer)
    precision = recall = None
    precision_detail, recall_detail = [], []
    if row["category"] in DATA_CATEGORIES:
        precision, precision_detail = context_precision(model, row["input"], row["expected_traits"], contexts)
        recall, recall_detail = context_recall(model, row["expected_traits"], contexts)
    return {
        "judge": {"모델": JUDGE_MODEL_ID, **judge},
        "metrics": {
            "faithfulness": faith,
            "answer_relevancy": relevancy,
            "context_precision": precision,
            "context_recall": recall,
            "세부": {
                "주장": claims,
                "생성 질문": generated,
                "근거 쓸모": precision_detail,
                "기준 답 항목": recall_detail,
            },
        },
    }


def _mean(values: list[float | None]) -> float | None:
    """빈 값을 뺀 평균. 값이 없으면 None."""
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def main() -> None:
    """결과 파일을 채점하고 같은 파일에 덧붙인 뒤 요약을 출력한다."""
    parser = argparse.ArgumentParser(description="평가 결과를 LLM-as-Judge 와 RAGAS 방식 지표로 채점한다")
    parser.add_argument("results", help="run_eval.py 가 만든 결과 JSON")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--force", action="store_true", help="이미 채점한 건도 다시 채점한다")
    args = parser.parse_args()

    path = Path(args.results)
    results = json.loads(path.read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in csv.DictReader(QUERIES.open(encoding="utf-8"))}
    by_id = {r["id"]: r for r in results}
    model = _chat()
    embedder = BedrockEmbeddings(
        model_id=EMBEDDING_MODEL_ID, region_name=os.getenv("AWS_DEFAULT_REGION"), config=RETRY_CONFIG,
    )
    lock = threading.Lock()

    def save() -> None:
        with lock:
            path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    def contexts_of(item: dict) -> list[str]:
        contexts = list(item["response"].get("contexts", []))
        prev = SESSION_CHAIN.get(item["id"])
        if prev in by_id:
            contexts += by_id[prev]["response"].get("contexts", [])
        return contexts

    def work(item: dict) -> dict:
        if "judge" in item and not args.force:
            return item
        scored = score_item(item, rows[item["id"]], contexts_of(item), model, embedder)
        item.update(scored)
        save()
        m, j = scored["metrics"], scored["judge"]
        print(f"{item['id']}: Judge {'통과' if j.get('통과') else '불통과'} | faith {m['faithfulness']} | "
              f"rel {m['answer_relevancy']} | prec {m['context_precision']} | recall {m['context_recall']}", flush=True)
        return item

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, results))

    save()
    passed = sum(bool(r["judge"].get("통과")) for r in results)
    print(f"\nJudge 통과 {passed}/{len(results)}")
    for key in ("faithfulness", "answer_relevancy", "context_precision", "context_recall"):
        print(f"{key} 평균 {_mean([r['metrics'][key] for r in results])}")
    print(f"저장: {path}")


if __name__ == "__main__":
    main()
