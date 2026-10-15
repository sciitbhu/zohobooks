"""
clients/claude_client.py
--------------------------
Thin wrapper around the official `anthropic` Python SDK, adding:
  * Retry/backoff on transient errors (rate limits, 5xx)
  * A structured JSON-classification helper for Track 1 (income categorization)
  * A long-form Markdown generation helper for Track 2 (EOM MIS report)

We use the synchronous Anthropic client wrapped by asyncio.to_thread() at
call sites in async code, because the officially supported async client
(AsyncAnthropic) is also available and used here directly for consistency
with the rest of the codebase's async style.
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from config import settings
from logging_setup import logger

# Valid categories Claude is allowed to assign in Track 1. Kept centrally so
# the webhook handler and the prompt template both reference one source of
# truth.
INCOME_CATEGORIES = ["B2B", "B2C", "Overseas", "Custom_Sector"]


class ClaudeClient:
    def __init__(self) -> None:
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type((APIStatusError, APIConnectionError)),
    )
    async def _create_message(self, **kwargs: Any):
        return await self._client.messages.create(**kwargs)

    # ------------------------------------------------------------------ #
    # Track 1: Real-time invoice categorization
    # ------------------------------------------------------------------ #
    async def classify_income(self, customer_payload: dict) -> dict:
        """
        Sends customer/invoice metadata to Claude and gets back a strict
        JSON classification: {"category": ..., "confidence": ..., "reasoning": ...}

        We force JSON-only output via prompt instructions and validate/
        parse defensively, since even well-instructed models can
        occasionally wrap output in prose or markdown fences.
        """
        from prompts.categorization_prompt import build_categorization_prompt

        system_prompt, user_prompt = build_categorization_prompt(customer_payload)

        logger.info("Sending invoice payload to Claude for income categorization.")
        response = await self._create_message(
            model=self.model,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()

        result = self._safe_json_parse(raw_text)

        category = result.get("category")
        if category not in INCOME_CATEGORIES:
            logger.warning(
                f"Claude returned an unexpected category '{category}'. "
                f"Falling back to 'Custom_Sector'. Raw response: {raw_text}"
            )
            result["category"] = "Custom_Sector"

        return result

    # ------------------------------------------------------------------ #
    # Track 2: EOM MIS report generation
    # ------------------------------------------------------------------ #
    async def generate_mis_report(self, financial_data: dict, period_label: str) -> str:
        """
        Feeds the aggregated Zoho Books financial data to Claude and returns
        a polished Markdown executive report as a string.
        """
        from prompts.mis_report_prompt import build_mis_report_prompt

        system_prompt, user_prompt = build_mis_report_prompt(financial_data, period_label)

        logger.info(f"Requesting MIS report generation from Claude for period: {period_label}")
        response = await self._create_message(
            model=self.model,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        markdown_text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()

        if not markdown_text:
            raise RuntimeError("Claude returned an empty MIS report — aborting save.")

        return markdown_text

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_json_parse(raw_text: str) -> dict:
        """
        Defensively parses JSON out of a model response, stripping common
        wrapping artifacts like ```json ... ``` fences.
        """
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            # Strip a leading "json" language hint if present.
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Claude JSON output. Raw text was:\n{raw_text}")
            # Fail safe rather than crashing the whole webhook — caller
            # will fall back to Custom_Sector via the category validation.
            return {"category": "Custom_Sector", "confidence": 0.0, "reasoning": "parse_error"}
