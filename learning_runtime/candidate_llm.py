"""Isolated OpenAI-compatible calls for candidate-mining functions."""

from __future__ import annotations

from typing import Any

from local_sdlc.llm_client import LocalLLMClient
from sdlc_events import canonical_json


class LocalCandidateLLM:
    def __init__(self, client: LocalLLMClient) -> None:
        self.client = client

    def complete(
        self,
        function_name: str,
        system_prompt: str,
        document: dict[str, object],
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": canonical_json(document)},
        ]
        return self.client.complete(
            messages,
            agent_level="judge",
            call_function=function_name,
        )

    def reasoning_audit(self) -> list[dict[str, Any]]:
        return [
            {
                "agent_level": item.get("agent_level"),
                "call_function": item.get("call_function"),
                "model": item.get("model"),
                "chars": item.get("chars"),
                "truncated": item.get("truncated"),
            }
            for item in self.client.reasoning_records
        ]
