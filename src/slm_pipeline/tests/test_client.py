"""test_client.py — Tests for LlamaServerClient with dual response shape parsing (U-4)."""

import json
import httpx
import pytest

from slm_pipeline.client import LlamaServerClient
from slm_pipeline.contracts import LlmError


@pytest.mark.asyncio
async def test_parse_shape_a_probs():
    """Shape A: completion_probabilities[0].probs[] with tok_str and prob."""
    mock_data = {
        "content": "start",
        "completion_probabilities": [
            {
                "probs": [
                    {"tok_str": "start", "prob": 0.85},
                    {"tok_str": "resume", "prob": 0.10},
                    {"tok_str": "stop", "prob": 0.05},
                ]
            }
        ],
    }

    async def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_data)

    transport = httpx.MockTransport(handle_request)
    client = LlamaServerClient(transport=transport)
    try:
        tokens = await client.score_first_token("test prompt", n_probs=3, timeout_s=1.0)
        assert len(tokens) == 3
        assert tokens[0].token == "start"
        assert tokens[0].prob == 0.85
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_parse_shape_b_top_logprobs():
    """Shape B: completion_probabilities[0].top_logprobs[] with token and logprob."""
    import math

    mock_data = {
        "content": "stop",
        "completion_probabilities": [
            {
                "top_logprobs": [
                    {"token": "stop", "logprob": math.log(0.90)},
                    {"token": "pause", "logprob": math.log(0.08)},
                ]
            }
        ],
    }

    async def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_data)

    transport = httpx.MockTransport(handle_request)
    client = LlamaServerClient(transport=transport)
    try:
        tokens = await client.score_first_token("test prompt", n_probs=2, timeout_s=1.0)
        assert len(tokens) == 2
        assert tokens[0].token == "stop"
        assert pytest.approx(tokens[0].prob, 0.01) == 0.90
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_error_http_503():
    async def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(handle_request)
    client = LlamaServerClient(transport=transport)
    try:
        with pytest.raises(LlmError) as exc_info:
            await client.score_first_token("prompt", n_probs=1, timeout_s=1.0)
        assert exc_info.value.kind == "HTTP_5XX"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_error_bad_body():
    async def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not valid json")

    transport = httpx.MockTransport(handle_request)
    client = LlamaServerClient(transport=transport)
    try:
        with pytest.raises(LlmError) as exc_info:
            await client.score_first_token("prompt", n_probs=1, timeout_s=1.0)
        assert exc_info.value.kind == "BAD_BODY"
    finally:
        await client.aclose()
