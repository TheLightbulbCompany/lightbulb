"""Rerank and question expansion, driven through botocore Stubbers on real clients."""

import boto3
import pytest
from botocore.stub import Stubber

from core.config import settings
from core.services import memory_models

ARN = f"arn:aws:bedrock:{settings.AWS_REGION}::foundation-model/cohere.rerank-v3-5:0"


def _client(service: str):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="x",
    )


@pytest.fixture
def stubbed(monkeypatch):
    """Install a stubbed client for a service; returns the Stubber."""

    def install(service: str) -> Stubber:
        client = _client(service)
        monkeypatch.setattr(memory_models, "_client", lambda s: client if s == service else None)
        stubber = Stubber(client)
        stubber.activate()
        return stubber

    return install


def _rerank_params(query: str, docs: list[str], top_n: int) -> dict:
    return {
        "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
        "sources": [
            {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d}}} for d in docs
        ],
        "rerankingConfiguration": {
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {"modelArn": ARN},
                "numberOfResults": top_n,
            },
        },
    }


async def test_rerank_returns_indices_sorted_by_score(stubbed):
    stub = stubbed("bedrock-agent-runtime")
    stub.add_response(
        "rerank",
        {"results": [{"index": 0, "relevanceScore": 0.4}, {"index": 2, "relevanceScore": 0.9}]},
        _rerank_params("q", ["a", "b", "c"], 2),
    )
    assert await memory_models.rerank("q", ["a", "b", "c"], 2) == [(2, 0.9), (0, 0.4)]
    stub.assert_no_pending_responses()


async def test_rerank_failure_returns_none(stubbed, caplog):
    stub = stubbed("bedrock-agent-runtime")
    stub.add_client_error("rerank", "ThrottlingException")
    assert await memory_models.rerank("secret question", ["secret doc"], 1) is None
    assert "secret" not in caplog.text


async def test_rerank_caps_documents_at_100(stubbed):
    stub = stubbed("bedrock-agent-runtime")
    docs = [f"d{i}" for i in range(130)]
    stub.add_response("rerank", {"results": []}, _rerank_params("q", docs[:100], 5))
    assert await memory_models.rerank("q", docs, 5) == []
    stub.assert_no_pending_responses()


async def test_rerank_empty_documents_skips_call(stubbed):
    stubbed("bedrock-agent-runtime")  # no responses queued: any call would raise
    assert await memory_models.rerank("q", [], 5) == []


def _converse_params(question: str) -> dict:
    return {
        "modelId": settings.MEMORY_EXPAND_MODEL,
        "system": [{"text": memory_models.EXPANSION_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": question}]}],
        "inferenceConfig": {"maxTokens": 200},
    }


def _reply(text: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


async def test_expand_single_line_returns_original(stubbed):
    stub = stubbed("bedrock-runtime")
    stub.add_response("converse", _reply("What is the launch date?\n"), _converse_params("when do we launch?"))
    assert await memory_models.expand("when do we launch?") == ["when do we launch?"]


async def test_expand_multiple_lines_capped_at_5(stubbed):
    stub = stubbed("bedrock-runtime")
    text = "\n".join(f" fact {i} " if i % 2 else f"fact {i}\n" for i in range(8))
    stub.add_response("converse", _reply(text), _converse_params("q"))
    assert await memory_models.expand("q") == [f"fact {i}" for i in range(5)]


async def test_expand_error_returns_original(stubbed):
    stub = stubbed("bedrock-runtime")
    stub.add_client_error("converse", "ThrottlingException")
    assert await memory_models.expand("q") == ["q"]


async def test_rerank_clamps_top_n_to_document_count(stubbed):
    stub = stubbed("bedrock-agent-runtime")
    stub.add_response("rerank", {"results": []}, _rerank_params("q", ["a", "b"], 2))
    assert await memory_models.rerank("q", ["a", "b"], 10) == []
    stub.assert_no_pending_responses()
