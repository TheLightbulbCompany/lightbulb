"""Model calls for workspace-memory recall: a Bedrock reranker and a one-call question expander.

Both degrade rather than raise: ``rerank`` returns None and ``expand`` returns the original
question, so recall falls back to its un-reranked / un-expanded path. Neither logs query or
document text.
"""

import functools
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

MAX_RERANK_DOCUMENTS = 100  # Rerank's per-call source cap
MAX_EXPANSIONS = 5
EXPANSION_PROMPT = (
    "List the separate facts this question needs, one per line, at most 5; if it needs one, write the question itself."
)
_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1})


@functools.cache  # one per process: a fresh client per call paid construction + TLS on the turn
def _client(service: str):
    return boto3.client(service, region_name=settings.AWS_REGION, config=_CONFIG)


async def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None:
    """(index into ``documents``, score) pairs, best first; None if Bedrock fails."""
    documents = documents[:MAX_RERANK_DOCUMENTS]
    if not documents:
        return []
    arn = f"arn:aws:bedrock:{settings.AWS_REGION}::foundation-model/cohere.rerank-v3-5:0"
    try:
        resp = await run_in_thread(
            _client("bedrock-agent-runtime").rerank,
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=[
                {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d}}}
                for d in documents
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": arn},
                    "numberOfResults": min(top_n, len(documents)),
                },
            },
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning("memory rerank failed: %s", type(exc).__name__)
        return None
    results = sorted(resp.get("results", []), key=lambda r: r["relevanceScore"], reverse=True)
    return [(r["index"], r["relevanceScore"]) for r in results]


async def expand(question: str) -> list[str]:
    """The separate facts ``question`` needs (at most 5), or ``[question]``."""
    try:
        resp = await run_in_thread(
            _client("bedrock-runtime").converse,
            modelId=settings.MEMORY_EXPAND_MODEL,
            system=[{"text": EXPANSION_PROMPT}],
            messages=[{"role": "user", "content": [{"text": question}]}],
            inferenceConfig={"maxTokens": 200},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    except (ClientError, BotoCoreError, KeyError) as exc:
        logger.warning("memory expand failed: %s", type(exc).__name__)
        return [question]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[:MAX_EXPANSIONS] if len(lines) > 1 else [question]
