"""
mis/generate_mis_report.py
-----------------------------
Track 2: Automated End-of-Month MIS report + macOS Calendar sync.

This is a standalone script (not part of the FastAPI app) meant to be
invoked directly by macOS `launchd` on the 1st of every month. It:

  1. Computes "previous month" as the reporting period.
  2. Pulls Expenses, Chart of Accounts, Inventory, Fixed Assets, P&L,
     Balance Sheet, and Bills (paid + pending/overdue) from Zoho Books.
  3. Sends the aggregated data to Claude to generate a Markdown report.
  4. Saves the report to ~/Documents/Zoho_MIS_Reports/YYYY/MM_Report.md
  5. Creates macOS Calendar reminders for every pending/overdue bill.

Run manually to test:
    python -m mis.generate_mis_report

Run for a specific historical month (useful for backfilling/testing):
    python -m mis.generate_mis_report --year 2026 --month 5
"""

from __future__ import annotations

import argparse
import asyncio
import calendar as pycalendar
from datetime import date, timedelta
from pathlib import Path

from config import settings
from logging_setup import logger
from clients.zoho_client import ZohoBooksClient, ZohoAPIError
from clients.claude_client import ClaudeClient
from mis.calendar_sync import sync_bills_to_calendar


def _previous_month_range(today: date | None = None) -> tuple[date, date, str]:
    """
    Returns (period_start, period_end, period_label) for "last month"
    relative to `today` (defaults to real today — overridable for tests/
    backfills via --year/--month CLI args).
    """
    today = today or date.today()
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    period_start = last_day_prev_month.replace(day=1)
    period_end = last_day_prev_month
    period_label = period_start.strftime("%B %Y")  # e.g. "June 2026"
    return period_start, period_end, period_label


async def _gather_financial_data(zoho: ZohoBooksClient, period_start: date, period_end: date) -> dict:
    """
    Pulls all the raw data points needed for the MIS report. Each pull is
    wrapped individually so a single unavailable module (e.g. Fixed Assets
    not enabled on this Zoho Books plan) doesn't abort the entire report —
    it degrades gracefully with an empty list + logged warning instead.
    """
    date_start_str = period_start.isoformat()
    date_end_str = period_end.isoformat()

    async def _safe(coro, label: str, default):
        try:
            return await coro
        except ZohoAPIError as exc:
            logger.warning(f"Could not fetch '{label}' from Zoho Books (continuing without it): {exc}")
            return default

    expenses = await _safe(zoho.list_expenses(date_start_str, date_end_str), "expenses", [])
    chart_of_accounts = await _safe(zoho.chart_of_accounts(), "chart_of_accounts", [])
    inventory = await _safe(zoho.inventory_summary(), "inventory", [])
    fixed_assets = await _safe(zoho.fixed_assets(), "fixed_assets", [])
    profit_and_loss = await _safe(
        zoho.profit_and_loss(date_start_str, date_end_str), "profit_and_loss", {}
    )
    balance_sheet = await _safe(zoho.balance_sheet(date_end_str), "balance_sheet", {})
    bills_paid = await _safe(zoho.list_bills(status="paid"), "bills_paid", [])
    bills_overdue = await _safe(zoho.list_bills(status="overdue"), "bills_overdue", [])
    bills_unpaid = await _safe(zoho.list_bills(status="unpaid"), "bills_pending", [])

    # Roll expenses up by Chart of Accounts category for a cleaner prompt
    # input (Claude can still see raw line items too, but this pre-aggregation
    # reduces token usage and gives Claude a head start).
    expenses_by_account: dict[str, float] = {}
    for exp in expenses:
        account_name = exp.get("account_name", "Uncategorized")
        amount = float(exp.get("total", 0) or 0)
        expenses_by_account[account_name] = expenses_by_account.get(account_name, 0.0) + amount

    return {
        "period_start": date_start_str,
        "period_end": date_end_str,
        "expenses_raw": expenses,
        "expenses_by_account": expenses_by_account,
        "chart_of_accounts": chart_of_accounts,
        "inventory": inventory,
        "fixed_assets": fixed_assets,
        "profit_and_loss": profit_and_loss,
        "balance_sheet": balance_sheet,
        "bills_paid": bills_paid,
        "bills_pending_overdue": bills_unpaid + bills_overdue,
    }


def _save_report(markdown_text: str, period_start: date) -> Path:
    """
    Saves to ~/Documents/Zoho_MIS_Reports/YYYY/MM_Report.md, creating
    directories as needed. Returns the final file path.
    """
    year_dir = settings.mis_report_root_path / f"{period_start.year}"
    year_dir.mkdir(parents=True, exist_ok=True)

    file_path = year_dir / f"{period_start.month:02d}_Report.md"
    file_path.write_text(markdown_text, encoding="utf-8")
    logger.success(f"MIS report saved to: {file_path}")
    return file_path


async def run(year: int | None = None, month: int | None = None) -> Path:
    """
    Main orchestration coroutine. If year/month are provided, generates the
    report for that specific historical month instead of "last month" —
    handy for backfills and manual testing.
    """
    if year and month:
        period_start = date(year, month, 1)
        period_end = date(year, month, pycalendar.monthrange(year, month)[1])
        period_label = period_start.strftime("%B %Y")
    else:
        period_start, period_end, period_label = _previous_month_range()

    logger.info(f"=== Starting EOM MIS report generation for {period_label} ===")

    async with ZohoBooksClient() as zoho:
        financial_data = await _gather_financial_data(zoho, period_start, period_end)

    claude = ClaudeClient()
    markdown_report = await claude.generate_mis_report(financial_data, period_label)

    report_path = _save_report(markdown_report, period_start)

    calendar_summary = sync_bills_to_calendar(financial_data["bills_pending_overdue"])
    logger.info(
        f"Calendar sync summary for {period_label}: "
        f"{calendar_summary['created']} created, {calendar_summary['failed']} failed."
    )

    logger.success(f"=== MIS report generation complete for {period_label} ===")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Zoho Books EOM MIS report.")
    parser.add_argument("--year", type=int, default=None, help="Backfill: report year (e.g. 2026)")
    parser.add_argument("--month", type=int, default=None, help="Backfill: report month (1-12)")
    args = parser.parse_args()

    try:
        asyncio.run(run(year=args.year, month=args.month))
    except Exception:
        logger.exception("MIS report generation failed with an unhandled exception.")
        raise  # non-zero exit code so launchd/monitoring can detect failure


if __name__ == "__main__":
    main()
