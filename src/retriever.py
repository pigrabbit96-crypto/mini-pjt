"""RAG 파이프라인.

data/ 의 설명 문서(.md)를 '##' 섹션 단위로 잘라 Bedrock 임베딩으로 메모리에 색인하고,
질문과 가까운 섹션을 찾아 답변 근거로 돌려준다. 문서에는 인구 숫자가 없고,
데이터 기준·지표 정의·코호트 추정 방법과 한계 같은 설명만 있다.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from langchain_aws import BedrockEmbeddings
from langchain_core.documents import Document
from langchain_core.tools import tool
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
TOP_K = 3


def load_documents() -> list[Document]:
    """data/ 의 마크다운 문서를 섹션 단위 Document 목록으로 자른다.

    섹션 제목은 본문에 남겨 두고, 출처 파일 이름과 섹션 제목을 메타데이터에 담는다.
    """
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "문서"), ("##", "섹션")],
        strip_headers=False,
    )
    docs: list[Document] = []
    for path in sorted(DATA_DIR.glob("*.md")):
        for chunk in splitter.split_text(path.read_text(encoding="utf-8")):
            chunk.metadata["출처"] = path.name
            docs.append(chunk)
    if not docs:
        raise FileNotFoundError(f"{DATA_DIR} 에 검색할 .md 문서가 없다")
    return docs


@lru_cache(maxsize=1)
def get_vector_store() -> InMemoryVectorStore:
    """문서를 임베딩해 메모리 벡터 저장소를 만든다. 처음 한 번만 만들고 캐시한다."""
    embeddings = BedrockEmbeddings(
        model_id=EMBEDDING_MODEL_ID,
        region_name=os.getenv("AWS_DEFAULT_REGION"),
    )
    store = InMemoryVectorStore(embeddings)
    store.add_documents(load_documents())
    return store


def retrieve(query: str, k: int = TOP_K) -> list[dict]:
    """질문과 가장 가까운 문서 섹션 k 개를 출처·섹션·내용으로 돌려준다."""
    results = []
    for doc in get_vector_store().similarity_search(query, k=k):
        results.append(
            {
                "출처": doc.metadata["출처"],
                "섹션": doc.metadata.get("섹션") or doc.metadata.get("문서", ""),
                "내용": doc.page_content,
            }
        )
    return results


@tool(parse_docstring=True)
def search_guide(query: str) -> dict:
    """데이터 설명 문서에서 질문과 관련된 섹션을 찾는다.

    지표의 정의(고령화율, 노년부양비, 중위연령 등), 데이터 기준 시점과 대상,
    데이터에 없는 항목, 시도 별칭, 강원·전북 행정구역 개편, 코호트 추정 방법과
    한계를 설명할 때 쓴다. 인구 숫자는 이 도구가 아니라 인구 도구에서 얻는다.

    Args:
        query: 찾을 내용 (예: "노년부양비 정의", "코호트 추정의 한계").
    """
    return {"검색 결과": retrieve(query)}
