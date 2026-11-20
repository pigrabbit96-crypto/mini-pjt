# 프로젝트 규칙

서비스 정의(사용자, 도구, 가드레일, 성공 기준)는 [SERVICE.md](SERVICE.md)를 따른다.

## 작업 규칙
- 무엇을 만드는지는 SERVICE.md가 기준이다. 애매하면 추측하지 말고 물어본다.
- 스택: Python, LangGraph, Bedrock (global.anthropic.claude-sonnet-4-6). 필요하면 모델을 조정하거나, 상황에 맞춰 여러 모델을 바꿔 가며 중단 없이 쓸 수 있게 한다.
- 괜찮은 레퍼런스 코드를 깃허브에서 찾아 참고하거나, ref 폴더가 있으면 그 안의 코드를 참조한다. 사용자의 허락 없이 새 프레임워크나 라이브러리를 들이지 않는다.
- 한 번에 TASKS.md의 작업 하나만 한다.
- 계산과 규칙 판정은 LLM이 아니라 파이썬 코드로 한다. 필요하면 LLM 판정을 덧붙인다.
- API 키는 .env에서 읽는다.

## 기술 스택
- Python, LangChain, LangGraph
- 모델은 Amazon Bedrock (ChatBedrockConverse)
- Agent 생성은 langchain.agents 의 create_agent 를 쓴다

## 폴더 구조 (제출 규약. 바꾸지 않는다)
```
src/agent.py       메인 에이전트 그래프
src/tools.py       도메인 도구
src/retriever.py   RAG 파이프라인
data/              사용한 문서와 데이터
evaluation/        test_queries.csv 와 평가 리포트
```

## 주고받는 형식 (제출 규약)
- POST /query 로 받고 question 필드를 읽는다
- 답은 answer, contexts, trace 세 키로 돌려준다

## 코드 규칙
- 파일 하나에 한 가지 역할만 둔다
- 함수와 도구에는 한국어 docstring 을 쓴다
- 비밀 값은 .env 에서 읽고 코드에 적지 않는다
- 답변 속 인구 숫자는 도구 계산 결과에서만 가져온다. 프롬프트나 코드에 숫자를 적어 두지 않는다
- 응답과 문서는 한국어로 쓴다

## 하지 말 것
- 요청하지 않은 파일을 새로 만들지 않는다
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다
- data/ 의 원본 엑셀은 수정하지 않는다
