"""
clients/zoho_client.py
-----------------------
Async Zoho Books API client.

Handles:
  * OAuth 2.0 access-token acquisition from the long-lived refresh token
  * Automatic re-authentication when a token expires (or a 401 is hit)
  * Thin, typed wrapper methods for every Zoho Books endpoint this project
    needs (invoices, expenses, inventory, chart of accounts, P&L, balance
    sheet, bills).

Design notes:
  * We cache the access token + its expiry in memory. Zoho access tokens
    are valid for ~3600s; we refresh proactively 60s before expiry AND
    reactively if a call still comes back 401 (belt and suspenders).
  * All network calls go through `_request()` so retry/backoff and auth
    injection happen in exactly one place.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from config import settings
from logging_setup import logger


class ZohoAuthError(RuntimeError):
    """Raised when Zoho OAuth token acquisition/refresh fails."""


class ZohoAPIError(RuntimeError):
    """Raised when a Zoho Books API call fails after retries."""


class ZohoBooksClient:
    """
    Usage:
        async with ZohoBooksClient() as zoho:
            invoice = await zoho.get_invoice(invoice_id)
    """

    def __init__(self) -> None:
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._http = httpx.AsyncClient(timeout=30.0)

    # ------------------------------------------------------------------ #
    # Context manager plumbing
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "ZohoBooksClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # OAuth token management
    # ------------------------------------------------------------------ #
    async def _ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing it if needed."""
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        logger.info("Zoho access token missing/expired — refreshing.")
        payload = {
            "refresh_token": settings.zoho_refresh_token,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        }
        try:
            resp = await self._http.post(settings.zoho_oauth_token_url, data=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.exception("Zoho OAuth refresh request failed.")
            raise ZohoAuthError(f"Failed to refresh Zoho access token: {exc}") from exc

        if "access_token" not in data:
            logger.error(f"Zoho OAuth refresh response missing access_token: {data}")
            raise ZohoAuthError(f"Unexpected Zoho OAuth response: {data}")

        self._access_token = data["access_token"]
        # expires_in is in seconds; refresh 60s early to be safe.
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = now + expires_in - 60
        logger.info(f"Zoho access token refreshed. Valid for ~{expires_in}s.")
        return self._access_token

    # ------------------------------------------------------------------ #
    # Low-level request helper (auth injection + retry + 401 recovery)
    # ------------------------------------------------------------------ #
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.TransportError, ZohoAPIError)),
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        _retried_after_401: bool = False,
    ) -> dict:
        token = await self._ensure_valid_token()
        url = f"{settings.zoho_books_base_url}{path}"
        params = dict(params or {})
        params["organization_id"] = settings.zoho_organization_id

        headers = {"Authorization": f"Zoho-oauthtoken {token}"}

        logger.debug(f"Zoho request: {method} {url} params={params}")
        resp = await self._http.request(
            method, url, params=params, json=json_body, headers=headers
        )

        # If the token was invalidated server-side, force one refresh+retry.
        if resp.status_code == 401 and not _retried_after_401:
            logger.warning("Zoho API returned 401 — forcing token refresh and retrying once.")
            self._access_token = None
            return await self._request(
                method, path, params=params, json_body=json_body, _retried_after_401=True
            )

        if resp.status_code >= 400:
            logger.error(f"Zoho API error {resp.status_code}: {resp.text}")
            raise ZohoAPIError(f"Zoho API {method} {path} failed: {resp.status_code} {resp.text}")

        return resp.json()

    # ------------------------------------------------------------------ #
    # Track 1: Invoice / customer helpers (webhook categorization)
    # ------------------------------------------------------------------ #
    async def get_invoice(self, invoice_id: str) -> dict:
        data = await self._request("GET", f"/invoices/{invoice_id}")
        return data.get("invoice", {})

    async def get_contact(self, contact_id: str) -> dict:
        data = await self._request("GET", f"/contacts/{contact_id}")
        return data.get("contact", {})

    async def update_invoice_custom_field(
        self, invoice_id: str, category: str
    ) -> dict:
        """
        Writes the AI-assigned category into the custom field configured
        via ZOHO_INVOICE_CUSTOM_FIELD_API_NAME.
        """
        field_name = settings.zoho_invoice_custom_field_api_name
        body = {
            "custom_fields": [
                {"api_name": field_name, "value": category}
            ]
        }
        logger.info(f"Updating invoice {invoice_id} custom field '{field_name}' -> '{category}'")
        return await self._request("PUT", f"/invoices/{invoice_id}", json_body=body)

    # ------------------------------------------------------------------ #
    # Track 2: EOM MIS report data pulls
    # ------------------------------------------------------------------ #
    async def list_expenses(self, date_start: str, date_end: str) -> list[dict]:
        data = await self._request(
            "GET",
            "/expenses",
            params={"date_start": date_start, "date_end": date_end, "per_page": 200},
        )
        return data.get("expenses", [])

    async def chart_of_accounts(self) -> list[dict]:
        data = await self._request("GET", "/chartofaccounts")
        return data.get("chartofaccounts", [])

    async def inventory_summary(self) -> list[dict]:
        """Current stock on hand + valuation per item."""
        data = await self._request("GET", "/items", params={"per_page": 200, "filter_by": "Status.Active"})
        return data.get("items", [])

    async def fixed_assets(self) -> list[dict]:
        """
        Zoho Books' Fixed Asset Management module (if enabled) exposes
        /fixedassets. If not enabled on the account, this call will
        error — generate_mis_report.py handles that gracefully.
        """
        data = await self._request("GET", "/fixedassets", params={"per_page": 200})
        return data.get("fixed_assets", [])

    async def profit_and_loss(self, date_start: str, date_end: str) -> dict:
        return await self._request(
            "GET",
            "/reports/profitandloss",
            params={
                "from_date": date_start,
                "to_date": date_end,
                "response_option": 1,  # 1 = JSON summary (not PDF)
            },
        )

    async def balance_sheet(self, as_of_date: str) -> dict:
        return await self._request(
            "GET",
            "/reports/balancesheet",
            params={"date": as_of_date, "response_option": 1},
        )

    async def list_bills(self, status: Optional[str] = None) -> list[dict]:
        """
        status can be e.g. 'overdue', 'unpaid' (pending), 'paid'.
        See Zoho Books docs -> Bills -> List Bills for supported filters.
        """
        params: dict[str, Any] = {"per_page": 200}
        if status:
            params["filter_by"] = f"Status.{status.capitalize()}"
        data = await self._request("GET", "/bills", params=params)
        return data.get("bills", [])
