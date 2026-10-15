"""
webhook/server.py
-------------------
Track 1: Real-time income categorization webhook listener.

Run locally with:
    uvicorn webhook.server:app --host 127.0.0.1 --port 8787 --reload

Zoho Books webhook setup:
    Zoho Books -> Settings -> Automation -> Webhooks -> New Webhook
    URL: https://<your-tunnel-or-public-host>/webhook/zoho/invoice?secret=<ZOHO_WEBHOOK_SHARED_SECRET>
    Trigger on: Invoice Created, Invoice Payment Received (pick what you need)
    Since Zoho's webhook config lets you send a custom query param, we use
    that as a lightweight shared-secret check (see `_verify_secret` below).
    For internet exposure from a Mac, front this with a tunnel such as
    Cloudflare Tunnel or ngrok — do not port-forward directly without TLS.

Design notes:
  * The endpoint responds 202 Accepted immediately after basic validation
    and processes the categorization + Zoho update in a background task.
    This matters because Zoho will retry/flag webhooks that don't respond
    quickly, and Claude API latency (~1-3s) plus a follow-up Zoho PUT call
    could otherwise cause timeouts under load.
  * All processing is logged with the invoice_id so you can trace a single
    event end-to-end in ./logs/app_YYYY-MM-DD.log.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import JSONResponse

from config import settings
from logging_setup import logger
from clients.zoho_client import ZohoBooksClient, ZohoAPIError, ZohoAuthError
from clients.claude_client import ClaudeClient

app = FastAPI(
    title="Zoho x Claude Financial Intelligence Engine",
    description="Real-time invoice income categorization webhook.",
    version="1.0.0",
)


def _verify_secret(secret: Optional[str]) -> None:
    """Rejects the request unless the shared secret matches configuration."""
    if not secret or secret != settings.zoho_webhook_shared_secret:
        logger.warning("Webhook received with missing/invalid shared secret — rejecting.")
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret.")


def _extract_customer_payload(zoho_event: dict, contact: dict, invoice: dict) -> dict:
    """
    Normalizes the fields Claude needs for classification out of Zoho's
    invoice + contact objects. Field names below follow Zoho Books' public
    API response schema as of this writing — validate against your account
    if Zoho changes field names.
    """
    billing_address = invoice.get("billing_address") or contact.get("billing_address") or {}
    return {
        "invoice_id": invoice.get("invoice_id"),
        "invoice_number": invoice.get("invoice_number"),
        "invoice_amount": invoice.get("total"),
        "currency": invoice.get("currency_code"),
        "company_name": contact.get("company_name") or contact.get("contact_name"),
        "tax_id": contact.get("gst_no") or contact.get("tax_id") or contact.get("vat_reg_no"),
        "billing_address": {
            "city": billing_address.get("city"),
            "state": billing_address.get("state"),
            "country": billing_address.get("country"),
            "zip": billing_address.get("zip"),
        },
    }


async def _process_invoice_categorization(invoice_id: str) -> None:
    """
    Background task: fetch full invoice + contact from Zoho, classify with
    Claude, and write the category back to the invoice custom field.

    Any exception here is caught and logged (not raised) because this runs
    detached from the original HTTP request — there is no client waiting
    to receive an error response.
    """
    try:
        async with ZohoBooksClient() as zoho:
            invoice = await zoho.get_invoice(invoice_id)
            if not invoice:
                logger.error(f"Invoice {invoice_id} not found in Zoho Books — aborting categorization.")
                return

            contact_id = invoice.get("customer_id")
            contact = await zoho.get_contact(contact_id) if contact_id else {}

            customer_payload = _extract_customer_payload({}, contact, invoice)
            logger.info(f"[invoice={invoice_id}] Extracted payload for classification: {customer_payload}")

            claude = ClaudeClient()
            classification = await claude.classify_income(customer_payload)
            logger.info(f"[invoice={invoice_id}] Claude classification result: {classification}")

            await zoho.update_invoice_custom_field(invoice_id, classification["category"])
            logger.success(
                f"[invoice={invoice_id}] Successfully tagged as "
                f"'{classification['category']}' (confidence={classification.get('confidence')})."
            )

    except (ZohoAPIError, ZohoAuthError) as exc:
        logger.error(f"[invoice={invoice_id}] Zoho error during categorization: {exc}")
    except Exception:
        logger.exception(f"[invoice={invoice_id}] Unexpected error during categorization.")


@app.get("/healthz")
async def health_check() -> dict:
    """Simple liveness probe — useful when running under launchd/monitoring."""
    return {"status": "ok"}


@app.post("/webhook/zoho/invoice")
async def zoho_invoice_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    secret: Optional[str] = Query(default=None),
) -> JSONResponse:
    """
    Receives Zoho Books invoice/payment event payloads.

    Zoho's webhook payload shape varies slightly by trigger type, but it
    reliably includes the invoice ID. We defensively look in a couple of
    likely locations rather than assuming one exact schema.
    """
    _verify_secret(secret)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("Webhook payload was not valid JSON.")
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    logger.debug(f"Raw Zoho webhook payload: {body}")

    invoice_id = (
        body.get("invoice_id")
        or body.get("invoice", {}).get("invoice_id")
        or body.get("data", {}).get("invoice_id")
    )

    if not invoice_id:
        logger.warning(f"Webhook payload missing invoice_id. Payload keys: {list(body.keys())}")
        raise HTTPException(status_code=422, detail="Payload did not contain an invoice_id.")

    logger.info(f"Accepted webhook for invoice_id={invoice_id}. Queuing background categorization.")
    background_tasks.add_task(_process_invoice_categorization, invoice_id)

    # Respond immediately; Zoho only needs a fast 2xx acknowledgement.
    return JSONResponse(status_code=202, content={"status": "accepted", "invoice_id": invoice_id})


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting webhook server on {settings.webhook_host}:{settings.webhook_port}")
    uvicorn.run(
        "webhook.server:app",
        host=settings.webhook_host,
        port=settings.webhook_port,
        reload=False,
    )
