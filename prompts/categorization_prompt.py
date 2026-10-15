"""
prompts/categorization_prompt.py
----------------------------------
Prompt template for Track 1: classifying an invoice's income into
B2B / B2C / Overseas / Custom_Sector based on customer metadata.

Kept isolated from claude_client.py so you can iterate on prompt wording
without touching API-calling code.
"""

import json


CATEGORIZATION_SYSTEM_PROMPT = """You are a financial classification assistant for an Indian small business \
using Zoho Books. Your only job is to classify a single invoice's income \
into exactly one of these four categories:

- "B2B": Business-to-business domestic sale (customer has a company name \
and/or a GSTIN/tax ID, billing address is domestic).
- "B2C": Business-to-consumer domestic sale (customer appears to be an \
individual, no tax ID, billing address is domestic).
- "Overseas": Customer's billing address or currency indicates an \
international/export transaction, regardless of B2B or B2C status.
- "Custom_Sector": Use only when the data is ambiguous, incomplete, or \
clearly does not fit the other three categories (e.g. missing address \
and missing tax ID and unclear currency).

Rules:
1. Overseas takes priority over B2B/B2C whenever the currency is not the \
   business's home currency (INR) OR the billing country is not India.
2. Respond with STRICT JSON ONLY. No prose, no markdown code fences, no \
   explanation outside the JSON object.
3. The JSON object must have exactly these keys:
   "category" (one of B2B, B2C, Overseas, Custom_Sector),
   "confidence" (float between 0 and 1),
   "reasoning" (one concise sentence explaining the decision).
"""


def build_categorization_prompt(customer_payload: dict) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) ready to pass to the Anthropic API.

    `customer_payload` is expected to look like:
        {
            "company_name": str | None,
            "tax_id": str | None,          # GSTIN / VAT / EIN etc.
            "billing_address": {
                "city": str, "state": str, "country": str, ...
            },
            "currency": str,               # ISO code, e.g. "INR", "USD"
            "invoice_id": str,
            "invoice_amount": float,
        }
    """
    user_prompt = (
        "Classify the following invoice's income category.\n\n"
        f"Customer data:\n{json.dumps(customer_payload, indent=2, default=str)}\n\n"
        "Respond with the JSON object as instructed."
    )
    return CATEGORIZATION_SYSTEM_PROMPT, user_prompt
