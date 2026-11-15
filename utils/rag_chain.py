"""Retrieval-grounded analysis as a plain runnable chain.

Replaces the legacy RetrievalQA chain. Two things that chain could not do:

- search with one text and reason over another. The chain takes a `query`
  (what is embedded, see utils/retrieval_query.py) and the `evidence` (the
  fenced transaction record the model assesses);
- say where its context came from. The fragments are numbered with their page
  in the context, and the result carries the fragment ids and pages.

    chain.invoke({"query": ..., "evidence": ...})
    -> {"query", "result", "source_documents", "sources"}

`evidence` defaults to the query, which is what a free-text question wants.
Built on langchain_core only, so it runs with any retriever and chat model
and is tested without a provider or a vector store.
"""

from __future__ import annotations

from operator import itemgetter
from typing import Any, Dict, List, Sequence

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough

ANALYST_INSTRUCTIONS = (
    "You are a senior forensic auditor specialising in AML/CTF. Assess the "
    "transaction record for structuring, layering, and jurisdictional risk "
    "against FATF standards and the retrieved regulatory context. Base your "
    "assessment only on the evidence provided and the retrieved context. If the "
    "evidence is insufficient for a confident judgement, say so in the rationale "
    "and lower your confidence rather than speculating."
)

NO_CONTEXT = "(no regulatory context was retrieved)"

# Every value is passed as a variable, so braces in a record or a fragment
# are never read as template fields.
_TEMPLATE = ChatPromptTemplate.from_messages([
    ("system", "{instructions}"),
    ("human",
     "Regulatory context, as numbered fragments of the indexed guidance:\n\n{context}\n\n"
     "{evidence}\n\n"
     "Refer to a fragment by its number when you rely on it. If the context does not cover "
     "a point, say so rather than guessing."),
])


def fragment_refs(documents: Sequence[Any]) -> List[Dict[str, Any]]:
    """Id and page of each retrieved fragment, in retrieval order."""
    refs = []
    for doc in documents:
        meta = getattr(doc, "metadata", None) or {}
        refs.append({"id": meta.get("_id") or getattr(doc, "id", None), "page": meta.get("page")})
    return refs


def format_fragments(documents: Sequence[Any]) -> str:
    """The fragments as numbered context blocks, each labelled with its page."""
    if not documents:
        return NO_CONTEXT
    blocks = []
    for number, (doc, ref) in enumerate(zip(documents, fragment_refs(documents)), start=1):
        page = f" (page {ref['page']})" if ref["page"] is not None else ""
        blocks.append(f"[{number}]{page}\n{doc.page_content.strip()}")
    return "\n\n".join(blocks)


def build_analysis_chain(llm: Any, retriever: Any,
                         instructions: str = ANALYST_INSTRUCTIONS) -> Runnable:
    """Retrieve for `query`, then have the model assess `evidence` against the fragments."""

    def prepare(payload: Dict[str, Any]) -> Dict[str, Any]:
        query = payload["query"]
        return {"query": query, "evidence": payload.get("evidence") or query,
                "instructions": instructions}

    def finish(state: Dict[str, Any]) -> Dict[str, Any]:
        documents = list(state["source_documents"])
        return {"query": state["query"], "result": state["result"],
                "source_documents": documents, "sources": fragment_refs(documents)}

    return (
        RunnableLambda(prepare)
        | RunnablePassthrough.assign(source_documents=itemgetter("query") | retriever)
        | RunnablePassthrough.assign(context=lambda state: format_fragments(state["source_documents"]))
        | RunnablePassthrough.assign(result=_TEMPLATE | llm | StrOutputParser())
        | RunnableLambda(finish)
    )
