# 미니 PJT: 내 또래 인구 이동 Agent

## 무엇을 푸나
"1998년생 전남 또래는 5년 동안 어떻게 됐어?"처럼 출생연도와 지역으로 물으면, 연도별 엑셀에 흩어진 주민등록 인구를 같은 출생 코호트로 이어 따라가 또래가 얼마나 늘고 줄었는지 숫자로 답한다.

대상 데이터는 행정안전부 주민등록 연령별 인구현황 2021~2025년(각 12월 말, 전국과 17개 시도, 0~99세와 100세 이상)이다. 서비스 정의는 [SERVICE.md](SERVICE.md), 작업 이력은 [TASKS.md](TASKS.md)에 있다.

## 활용한 패턴 (Day 1~7)

- **Day 1: LCEL chain (Pydantic 구조화 출력)** — 부분 적용. `POST /query`의 요청·응답을 Pydantic 모델(`QueryRequest`, `QueryResponse`)로 정의해, 답변을 `answer`·`contexts`·`trace` 세 키의 dict로 돌려준다. 오류 응답도 같은 형식이다. 답변 생성 자체는 LCEL 체인이 아니라 에이전트가 맡는다.
- **Day 2: RAG** — 부분 적용. 지표 정의, 코호트 추정 방법과 한계, 행정구역 개편, 데이터 기준을 적은 설명 문서([data/population_guide.md](data/population_guide.md))를 `##` 섹션 단위로 Titan 임베딩해 벡터 검색한다(`search_guide`). 검색 결과는 응답의 `contexts`에 담긴다. 문서가 섹션 10개짜리 하나라 하이브리드 검색·리랭킹·쿼리 확장은 넣지 않았다.
- **Day 3: ReAct (도구 자율 선택)** — LangChain `create_agent`에 도메인 도구 6개를 붙인다. 모델이 질문을 보고 도구를 골라 부르고, 결과를 보고 다음 도구를 부를지 정한다. 계산은 모두 파이썬 도구가 하고, 모델은 도구 선택과 답변 문장만 맡는다.
- **Day 4: 도구 다중** — 한 질의에 도구를 2개 이상 엮어 쓴다. 예를 들어 "순변화 추정은 어떻게 계산해? 1995년생 부산으로 예를 들어줘"는 `search_guide`로 계산식을 찾고 `track_cohort`로 값을 구한다. 두 연도 지표 비교는 `get_indicators`를 연도마다 부른다. 데이터가 로컬 엑셀이라 DB·외부 API 도구는 없다.
- **Day 5: 가드레일** — 입력과 출력 양쪽에 둔다.
  - 입력: 프롬프트 인젝션(시스템 프롬프트·자격 증명 요구)과 특정 개인 찾기는 프롬프트 규칙으로 거절하고, 범위 밖 질문(시군구·성별·장래 예측)은 가능한 범위를 안내한다. 요청 형식 오류는 Pydantic 검증으로 막는다.
  - 출력: 답변을 내보내기 전에 파이썬으로 세 가지를 점검한다. 어긋나면 한 번 다시 쓰게 하고, 그래도 어긋나면 답변을 보류한다.
    - 숫자 검증: 답변 숫자를 도구 결과와 대조한다. 만 단위 어림과 '약·가량'도 잡는다.
    - 이동 경로 단정 점검: "서울로 갔다" 같은 표현을 잡는다.
    - 해석 문장 점검: 고령사회 단계, 기준값 비교, 정도 표현을 도구 결과와 대조한다.
  - 입력받는 개인정보는 출생연도와 시도뿐이라 PII 마스킹은 넣지 않았다.
- **Day 7: Observability · Trace** — 부분 적용. 외부 도구(LangSmith·LangFuse) 대신 응답의 `trace`에 직접 기록한다. 도구 호출(입력·출력)과 점검 단계별 결과(처음·최종 걸린 항목, 다시 쓰기 여부, 통과·보류)가 남고, 질문 화면의 "근거 보기"로도 볼 수 있다.
- **Day 7: 평가 (RAGAS · LLM-as-Judge)** — 인-아웃 세트 16건으로 평가를 돌린다. 채점은 LLM-as-Judge와 RAGAS 방식 지표 4개로 하며, `ragas` 라이브러리 없이 지표 정의를 직접 구현했다. 약점을 하나 고르고, 고친 뒤 다시 평가하는 라운드를 6번 돌렸다.
- **(대화 메모리)** — LangGraph `InMemorySaver`를 `session_id`별 thread로 써서 "그럼 서울은?" 같은 후속 질문이 앞 질문의 출생연도를 이어받는다. 세션 안의 단기 메모리이고, Day 7의 장기 메모리(LangGraph Store)는 아니다.

쓰지 않은 패턴과 이유는 다음과 같다.
- **MCP 서버 연동 (Day 4)**: 연동할 사내 시스템이 없고, 데이터는 로컬 엑셀 5개다.
- **HITL (Day 5)**: 조회만 하고 데이터를 바꾸거나 밖으로 보내는 위험 작업이 없다.
- **미들웨어 (Day 5)**: 다시 쓰기·보류는 `run_agent` 안에 직접 구현했다. 대화가 짧아 이력 요약이 필요 없고, 마스킹할 PII도 없다.
- **Multi-Agent Supervisor (Day 6)**: 도구 6개를 에이전트 하나가 고르는 규모라 역할을 나눌 필요가 없었다.
- **Plan-Execute · 장기 메모리 (Day 7)**: 질문 하나가 도구 1~3번 호출로 끝나 단계 분해가 필요 없다. 세션을 넘는 기억은 입력 개인정보를 저장하지 않는다는 정책(SERVICE.md)과 맞지 않는다.

## 아키텍처

```
브라우저 (GET /, src/web/index.html)          클라이언트 (POST /query)
                 │                                     │
                 └──────────────┬──────────────────────┘
                                ▼
          FastAPI  src/agent.py  ── question, session_id
                                │
                                ▼
          create_agent (Bedrock Claude Sonnet 4.6, InMemorySaver)
                                │  도구 호출
        ┌──────────┬────────────┼─────────────┬──────────────┬──────────────┐
        ▼          ▼            ▼             ▼              ▼              ▼
  track_cohort get_population get_indicators rank_regions get_data_coverage search_guide
        └──────────┴────────────┴─────────────┴──────────────┘              │
                 src/tools.py  (data/*.xlsx 5개를 읽어 계산)        src/retriever.py
                                                                  (population_guide.md
                                                                   섹션 임베딩 검색)
                                │
                                ▼
          답변 점검 (파이썬): 숫자 검증 → 이동 경로 표현 → 해석 문장
             어긋나면 한 번 다시 쓰기 → 그래도 어긋나면 보류
                                │
                                ▼
          응답 { answer, contexts, trace }
```

| 도구 | 하는 일 |
|---|---|
| `track_cohort` | 출생연도·시도로 2021→2025년 연도별 인원, 변화율, 전국 대비 차이, 국내 순변화 추정 |
| `get_population` | 연도·시도·나이 구간 인구수와 구성비. 비교 연도를 주면 변화(명)·변화율(%)·구성비 변화(%p) |
| `get_indicators` | 고령화율, 부양비, 노령화지수, 중위·평균연령, 고령사회 단계 |
| `rank_regions` | 인구수·비율·지표로 시도 순위, 고른 시도끼리 비교, 두 연도 변화량 순위 |
| `get_data_coverage` | 사용 가능한 연도·시도 이름과 별칭·나이 범위, 없는 데이터 안내 |
| `search_guide` | 설명 문서 검색 (RAG) |

## 실행 방법

Python 3.14에서 확인했다.

```bash
pip install -r requirements.txt
```

프로젝트 루트에 `.env`를 만든다. 이 파일은 커밋하지 않는다.

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
# 선택. 비우면 global.anthropic.claude-sonnet-4-6
BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6
```

서버를 켠다.

```bash
uvicorn src.agent:app --port 8000
```

- 질문 화면: 브라우저로 http://127.0.0.1:8000/ 을 연다. 후속 질문을 이어서 물을 수 있고, "근거 보기"로 도구 호출과 점검 결과를 볼 수 있다.
- API:

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "1998년생 전남 또래는 5년 동안 어떻게 됐어?", "session_id": "demo"}'
```

평가를 다시 돌리려면 서버를 켠 상태에서 다음을 실행한다.

```bash
python evaluation/run_eval.py --out evaluation/round7_results.json   # 16건 실행 (모델 호출)
python evaluation/judge.py evaluation/round7_results.json             # Judge·RAGAS 방식 지표 채점
python evaluation/check_numbers.py evaluation/round7_results.json     # 답변 숫자와 도구 결과 대조
python evaluation/check_excel.py                                      # 도구 결과와 원본 엑셀 대조
python evaluation/round_report.py 7                                   # 라운드 리포트 (rounds.json 에 라운드 7 설명 필요)
```

## RAGAS 평가 결과

라운드 6(최종 코드), 인-아웃 세트 16건 기준이다. 전체 결과는 [evaluation/round6_report.md](evaluation/round6_report.md)에 있다.

- context_recall: 1.00
- context_precision: 0.98
- faithfulness: 0.99
- answer_relevancy: 0.55

지표를 읽을 때 알아 둘 점은 다음과 같다.
- **구현 방식**: `ragas` 라이브러리를 쓰지 않고, RAGAS의 지표 정의를 Bedrock 채점 모델(Claude Haiku 4.5, 평가 대상과 다른 모델)과 Titan 임베딩으로 직접 구현했다([evaluation/judge.py](evaluation/judge.py)).
- **context 지표의 적용 범위**: context_recall·precision은 데이터를 조회해야 하는 질문(positive, edge 12건)에만 계산한다. 근거(`contexts`)는 도구 결과와 검색된 문서 섹션이다.
- **answer_relevancy가 낮은 이유**: 거절 답변(범위 밖·인젝션 질문)과 "그럼 서울은?" 같은 짧은 후속 질문은 RAGAS 정의상 점수가 낮게 나온다. 데이터 질문(12건)만 보면 0.24~0.86 범위다.

## 인-아웃 세트 통과율 (자체 평가)

세트는 [evaluation/test_queries.csv](evaluation/test_queries.csv)에 있다. 처음 12건(positive 5, negative 2, edge 3, guardrail 2)으로 시작했고, 지표·순위 도구를 넣으면서 16건이 됐다.

- 1차 (라운드 1, 첫 평가, 12건): **11 / 12 통과** (Judge 11/12, 사람 판정 11/12)
- 2차 (라운드 6, 개선 후, 16건): **16 / 16 통과** (Judge). 처음 12건만 보면 12 / 12
- 개선폭: 같은 12건 기준 **+1건** (11 → 12). 늘어난 4건(지표·순위 질문)도 모두 통과했다.
  - 주요 개선 사항: 이동 경로 단정 차단(라운드 1 E03 실패 원인), 정확한 값 앞의 '약' 차단, 해석 문장과 도구 결과 대조, 근거 없는 정도 표현 차단(라운드 5 E04 불통과 원인)

| 라운드 | 세트 | Judge 통과 | 무엇을 고친 뒤인가 | 리포트 |
|---|---|---|---|---|
| 1 | 12건 | 11/12 | 첫 평가 | [round1](evaluation/round1_report.md) |
| 2 | 12건 | 12/12 | 이동 경로 단정 차단 | [round2](evaluation/round2_report.md) |
| 3 | 12건 | 12/12 | 오류 응답도 세 키 | 채점 결과만 |
| 4 | 16건 | 16/16 | '약' 차단, 지표·순위 도구, 평가 4건 추가 | 채점 결과만 |
| 5 | 16건 | 15/16 | 간접 의문 오탐 수정, 해석 문장 대조 | 채점 결과만 |
| 6 | 16건 | 16/16 | 근거 없는 정도 표현 차단 | [round6](evaluation/round6_report.md) |

라운드별 원본 응답과 채점 결과는 `evaluation/roundN_results.json`에 있다. 라운드 1~5는 하루에 이어서 돌린 평가를 라운드로 나눈 것이고, 채점은 모두 같은 채점 모델로 했다.

## 트라이앤에러 회고

### 시도했지만 실패한 접근 · 왜 실패했는가

- **프롬프트 규칙만으로 금지 표현 막기**: "이동 경로를 단정하지 않는다"는 규칙이 있었지만 E03이 "꽤 많이 갔습니다"로 답해 실패했다. 추상적인 규칙은 모델이 해석해 비켜 간다.
- **숫자 검증만으로 정확성 보장하기**: 숫자 대조는 통과해도 문제가 남았다.
  - 어림 표현이 빠져나갔다. "50만"은 50으로 읽혀 검증 대상에서 빠졌고, "약 2,461명"처럼 정확한 값에 '약'을 붙여도 통과했다.
  - 해석이 어긋나도 걸러지지 않았다. "이미 초고령사회인 전남이 초고령 수준에 가깝다", "50.0세가 50세를 넘었다" 같은 문장이 나갔다.
- **LLM Judge만으로 판정하기**: 라운드 1에서 같은 이동 경로 단정을 사람은 E03에서, Judge는 P02에서 잡아 판정이 엇갈렸다. 라운드 5에서는 Judge가 근거 있는 주장을 근거 없다고 보는 오판도 있었다.
- **어휘 목록 점검의 오탐**: "어디로 옮겨 갔는지"처럼 두 어절로 나뉜 간접 의문을 단정으로 잡아 불필요한 다시 쓰기가 생겼다.
- **모델 하나에 고정하기**: CLAUDE.md에 적었던 Claude 3.5 Sonnet은 지원이 끝났고, Sonnet 4.6·4.5도 평가를 반복하며 하루 토큰 한도에 걸렸다.

### 최종 채택한 접근 · 왜 그것으로 갔는가

- **규칙은 파이썬으로 판정한다**: 금지 표현, 어림 표현, 해석 문장을 파이썬으로 점검해 걸리면 한 번 다시 쓰게 하고, 그래도 남으면 보류한다. 결과를 trace에 남겨 어디서 걸렸는지 볼 수 있다. 규칙이 구체적이어서 같은 입력에 늘 같은 판정이 나온다.
- **프롬프트는 구체적으로 쓰고, 파이썬 점검은 안전장치로 둔다**: 금지할 표현을 직접 나열하자 최근 라운드에서는 대부분 첫 답변부터 통과했다. 파이썬 점검은 프롬프트가 뚫릴 때를 위해 남겨 둔다.
- **Judge와 사람 판정, 결정적 대조를 함께 본다**: LLM Judge와 RAGAS 방식 지표로 넓게 보고, 숫자 대조와 원본 엑셀 대조는 스크립트로 확인한다. 엑셀 대조는 프로젝트의 엑셀 읽기 코드를 거치지 않고 xlsx를 직접 읽는다.
- **모델은 설정으로 바꾼다**: 모델 ID는 `.env`의 `BEDROCK_MODEL_ID`로 바꾼다. `global.`·`us.` 경로는 한도를 따로 세므로, 같은 모델을 다른 경로로 불러 작업을 이어 갔다.
- **약점 하나씩 고치는 라운드**: 채점에서 약점 하나를 골라 고치고 다시 평가한다. 바뀐 점과 지표 변화를 라운드 리포트로 남긴다.

### 남은 한계 · 향후 개선 방향

- **정의를 검색 없이 쓰는 경우**: 라운드 6 P01은 "고령화율 20% 이상이면 초고령사회"를 문서 검색 없이 써서 faithfulness가 0.8이다. 정의를 말할 때 검색하도록 하거나, 정의 문장이 근거에 있는지 점검해야 한다.
- **필요 없는 문서 섹션 검색**: 문서 검색이 섹션 3개를 가져와 관련 없는 섹션이 섞인다(P05 context precision 0.75). 검색 개수나 유사도 하한을 조정할 수 있다.
- **해석 문장 검증의 범위**: 단계 표현, 기준값 비교, 정도 표현 세 유형만 본다. "전북의 감소 폭이 가장 크다"처럼 기준을 밝히지 않은 비교는 아직 걸러지지 않는다.
- **1,000 미만 정수**: 나이·개수와 구분이 어려워 자동 검증하지 않는다.
- **실행마다 다른 답**: 도구 선택과 문장이 실행마다 달라진다(E04는 rank_regions와 get_indicators를 번갈아 부름). 평가는 라운드마다 한 번씩만 돌렸다.
- **채점 모델의 한계**: Haiku 4.5 채점에 오판이 있다. 한도가 풀리면 Sonnet 4.5로 다시 채점해 비교한다. 사람 판정은 라운드 1, 3, 4, 5에만 있다.
- **대화 기록**: 대화가 서버 메모리(`InMemorySaver`)에 남는다. 서버를 다시 켜면 세션이 사라지고, 질문 원문은 서버가 꺼질 때까지 메모리에 있다.

## 핵심 코드 위치

- `src/agent.py:177` — 메인 에이전트 그래프 (`get_agent`: Bedrock 모델, 도구, 시스템 프롬프트, 메모리)
- `src/agent.py:46` — 시스템 프롬프트 (숫자·도구·코호트·답변 형식·보안 규칙)
- `src/agent.py:448` — 질문 하나 처리와 답변 점검·다시 쓰기·보류 (`run_agent`)
- `src/agent.py:224`, `:267`, `:332` — 숫자 검증, 이동 경로 표현 점검, 해석 문장 점검
- `src/agent.py:580` — `POST /query`, `src/agent.py:558` — 질문 화면 `GET /`
- `src/tools.py` — 도메인 도구 (`track_cohort` :483, `get_population` :325, `get_indicators` :381, `rank_regions` :405, `get_data_coverage` :305, 등록 목록 `TOOLS` :548)
- `src/retriever.py` — RAG 파이프라인 (섹션 분할 :25, 벡터 저장소 :45, `search_guide` :71)
- `src/web/index.html` — 질문 입출력 화면
- `evaluation/` — 인-아웃 세트, 평가 실행·채점·대조 스크립트, 라운드별 리포트
