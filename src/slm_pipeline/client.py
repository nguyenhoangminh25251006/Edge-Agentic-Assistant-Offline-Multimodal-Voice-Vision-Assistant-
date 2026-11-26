"""client.py — llama-server HTTP client adapter with dual response shape parsing."""

from __future__ import annotations

import math
from typing import Any
import httpx

from slm_pipeline.contracts import LlmError, TokenProb


class LlamaServerClient:
    """Async client communicating with llama-server via httpx."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        connect_timeout_s: float = 0.5,
        request_timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            limits=httpx.Limits(max_connections=2),
            transport=transport,
            timeout=None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def score_first_token(
        self,
        prompt: str,
        *,
        n_probs: int = 10,
        timeout_s: float = 5.0,
    ) -> list[TokenProb]:
        """Request single first token probabilities from llama-server."""
        payload = {
            "prompt": prompt,
            "n_predict": 1,
            "temperature": 0,
            "seed": 0,
            "n_probs": n_probs,
            "cache_prompt": True,
            "stream": False,
        }

        try:
            resp = await self._client.post(
                "/completion",
                json=payload,
                timeout=httpx.Timeout(timeout_s, connect=self._connect_timeout_s),
            )
        except httpx.TimeoutException as exc:
            raise LlmError("TIMEOUT", str(exc)) from exc
        except httpx.ConnectError as exc:
            raise LlmError("CONNECT", str(exc)) from exc
        except httpx.RequestError as exc:
            raise LlmError("CONNECT", str(exc)) from exc

        if resp.status_code >= 500:
            raise LlmError("HTTP_5XX", f"Status {resp.status_code}: {resp.text}")
        if resp.status_code != 200:
            raise LlmError("BAD_BODY", f"Unexpected status {resp.status_code}")

        try:
            data = resp.json()
        except Exception as exc:
            raise LlmError("BAD_BODY", f"Invalid JSON response: {exc}") from exc

        return self._parse_probabilities(data)

    @staticmethod
    def _parse_probabilities(data: dict[str, Any]) -> list[TokenProb]:
        """Parse probabilities supporting Shape A and Shape B."""
        completion_probs = data.get("completion_probabilities")
        if not completion_probs or not isinstance(completion_probs, list) or len(completion_probs) == 0:
            raise LlmError("BAD_BODY", "Missing completion_probabilities")

        first_step = completion_probs[0]
        results: list[TokenProb] = []

        # Shape A: probs[] with tok_str and prob
        if "probs" in first_step and isinstance(first_step["probs"], list):
            for item in first_step["probs"]:
                tok = item.get("tok_str") or item.get("token") or ""
                prob = item.get("prob")
                if prob is None or not (0.0 <= prob <= 1.0001):
                    raise LlmError("BAD_BODY", f"Invalid probability value: {prob}")
                results.append(TokenProb(token=tok, prob=float(prob)))

        # Shape B: top_logprobs[] with token and logprob
        elif "top_logprobs" in first_step and isinstance(first_step["top_logprobs"], list):
            for item in first_step["top_logprobs"]:
                tok = item.get("token") or item.get("tok_str") or ""
                logprob = item.get("logprob")
                if logprob is None:
                    raise LlmError("BAD_BODY", "Missing logprob in top_logprobs")
                prob = math.exp(float(logprob))
                if not (0.0 <= prob <= 1.0001):
                    raise LlmError("BAD_BODY", f"Invalid calculated probability: {prob}")
                results.append(TokenProb(token=tok, prob=min(prob, 1.0)))
        else:
            raise LlmError("BAD_BODY", "Unknown completion_probabilities format")

        results.sort(key=lambda tp: tp.prob, reverse=True)
        return results

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 96,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        timeout_s: float = 8.0,
    ) -> str:
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "stop": stop or [],
            "cache_prompt": True,
            "stream": False,
        }
        try:
            resp = await self._client.post(
                "/completion",
                json=payload,
                timeout=httpx.Timeout(timeout_s, connect=self._connect_timeout_s),
            )
        except httpx.TimeoutException as exc:
            raise LlmError("TIMEOUT", str(exc)) from exc
        except httpx.RequestError as exc:
            raise LlmError("CONNECT", str(exc)) from exc

        if resp.status_code >= 500:
            raise LlmError("HTTP_5XX", f"Status {resp.status_code}")
        if resp.status_code != 200:
            raise LlmError("BAD_BODY", f"Unexpected status {resp.status_code}")

        try:
            data = resp.json()
            return str(data.get("content", ""))
        except Exception as exc:
            raise LlmError("BAD_BODY", f"Failed to parse generation content: {exc}") from exc

    async def tokenize(self, text: str) -> list[int]:
        try:
            resp = await self._client.post(
                "/tokenize",
                json={"content": text},
                timeout=httpx.Timeout(3.0, connect=self._connect_timeout_s),
            )
            if resp.status_code != 200:
                raise LlmError("BAD_BODY", f"Tokenize returned {resp.status_code}")
            data = resp.json()
            return [int(t) for t in data.get("tokens", [])]
        except Exception as exc:
            if isinstance(exc, LlmError):
                raise
            raise LlmError("CONNECT", str(exc)) from exc

    async def health(self) -> bool:
        try:
            resp = await self._client.get(
                "/health",
                timeout=httpx.Timeout(2.0, connect=self._connect_timeout_s),
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def props(self) -> dict[str, Any]:
        try:
            resp = await self._client.get(
                "/props",
                timeout=httpx.Timeout(2.0, connect=self._connect_timeout_s),
            )
            if resp.status_code == 200:
                return resp.json()
            return {}
        except Exception:
            return {}
