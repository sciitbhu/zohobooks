"""
mis/calendar_sync.py
----------------------
Creates native macOS Calendar events/reminders for each pending or overdue
bill, so payment/reminder-email deadlines show up where you'll actually see
them.

Implementation choice: AppleScript via `subprocess.run(["osascript", ...])`
rather than PyObjC/EventKit. Reasoning documented for maintainability:
  * Zero extra dependencies (osascript ships with every Mac).
  * No macOS "grant EventKit access to this Python interpreter" entitlement
    dance, which is fragile across venvs/launchd contexts.
  * Trivial to test by pasting the generated script into Script Editor.

If you later want deeper Calendar features (recurrence rules, alarms with
multiple offsets, attendee invites), migrate to PyObjC's EventKit — the
public function signature below (`sync_bills_to_calendar`) is intentionally
implementation-agnostic so that swap wouldn't ripple through the caller.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from typing import Optional

from logging_setup import logger

# Name of the macOS Calendar (as shown in Calendar.app's sidebar) events
# will be created in. Change this to an existing calendar name, or the
# script will create it if missing (see _ensure_calendar_exists).
TARGET_CALENDAR_NAME = "Zoho Bills & Payments"


def _escape_applescript_string(value: str) -> str:
    """Escapes double quotes/backslashes so untrusted text (vendor names,
    bill numbers) can't break out of the AppleScript string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str) -> str:
    """
    Executes an AppleScript string via osascript and returns stdout.
    Raises RuntimeError with stderr content on failure so callers can log
    and continue rather than crashing the whole report run over one bad
    calendar event.
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _ensure_calendar_exists(calendar_name: str) -> None:
    """Creates the target Calendar if it doesn't already exist."""
    script = f'''
    tell application "Calendar"
        if not (exists calendar "{calendar_name}") then
            make new calendar with properties {{name:"{calendar_name}"}}
        end if
    end tell
    '''
    _run_applescript(script)


def _create_calendar_event(
    title: str,
    notes: str,
    due_date: datetime,
    calendar_name: str = TARGET_CALENDAR_NAME,
    alarm_minutes_before: int = 60 * 9,  # default: alarm 9 hours before (previous evening-ish)
) -> None:
    """
    Creates a single all-day-ish reminder event on `due_date` with a
    display alarm. Times are formatted for AppleScript's `date` coercion,
    which is locale-sensitive — we use the numeric `current date` + set
    approach below to avoid locale-format parsing bugs entirely.
    """
    safe_title = _escape_applescript_string(title)
    safe_notes = _escape_applescript_string(notes)

    # Build the event at 09:00 on the due date to keep it visible/actionable
    # without being a literal all-day block.
    event_dt = due_date.replace(hour=9, minute=0, second=0, microsecond=0)

    script = f'''
    tell application "Calendar"
        tell calendar "{calendar_name}"
            set startDate to (current date)
            set year of startDate to {event_dt.year}
            set month of startDate to {event_dt.month}
            set day of startDate to {event_dt.day}
            set hours of startDate to {event_dt.hour}
            set minutes of startDate to {event_dt.minute}
            set seconds of startDate to 0

            set endDate to startDate + (30 * minutes)

            set newEvent to make new event with properties {{summary:"{safe_title}", ¬
                start date:startDate, end date:endDate, description:"{safe_notes}"}}

            tell newEvent
                make new display alarm at end of display alarms with properties ¬
                    {{trigger interval:-{alarm_minutes_before}}}
            end tell
        end tell
    end tell
    '''
    _run_applescript(script)


def sync_bills_to_calendar(
    pending_or_overdue_bills: list[dict],
    calendar_name: str = TARGET_CALENDAR_NAME,
) -> dict:
    """
    Public entry point called from generate_mis_report.py.

    `pending_or_overdue_bills` — list of dicts shaped like Zoho Books'
    `/bills` response items, expected to contain at minimum:
        bill_id, bill_number, vendor_name, due_date (YYYY-MM-DD), total,
        status ("overdue" | "unpaid" / pending), currency_code

    Returns a summary dict: {"created": n, "failed": n, "failures": [...]}
    for logging/reporting back to the caller.
    """
    if not pending_or_overdue_bills:
        logger.info("No pending/overdue bills to sync to macOS Calendar.")
        return {"created": 0, "failed": 0, "failures": []}

    try:
        _ensure_calendar_exists(calendar_name)
    except RuntimeError as exc:
        logger.error(f"Could not create/verify Calendar '{calendar_name}': {exc}")
        return {"created": 0, "failed": len(pending_or_overdue_bills), "failures": [str(exc)]}

    created, failed, failures = 0, 0, []

    for bill in pending_or_overdue_bills:
        vendor = bill.get("vendor_name", "Unknown Vendor")
        bill_number = bill.get("bill_number", "N/A")
        amount = bill.get("total", "N/A")
        currency = bill.get("currency_code", "")
        status = bill.get("status", "pending").upper()
        due_date_str: Optional[str] = bill.get("due_date")

        if not due_date_str:
            logger.warning(f"Bill {bill_number} has no due_date — skipping calendar sync.")
            failed += 1
            failures.append(f"{bill_number}: missing due_date")
            continue

        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"Bill {bill_number} has unparseable due_date '{due_date_str}' — skipping.")
            failed += 1
            failures.append(f"{bill_number}: unparseable due_date")
            continue

        title = f"[{status}] Pay {vendor} — Bill #{bill_number}"
        notes = (
            f"Vendor: {vendor}\n"
            f"Bill Number: {bill_number}\n"
            f"Amount Due: {amount} {currency}\n"
            f"Status: {status}\n"
            f"Source: Zoho Books automated MIS sync"
        )

        try:
            _create_calendar_event(title, notes, due_date, calendar_name=calendar_name)
            created += 1
            logger.info(f"Created Calendar reminder for bill {bill_number} (due {due_date_str}).")
        except RuntimeError as exc:
            failed += 1
            failures.append(f"{bill_number}: {exc}")
            logger.error(f"Failed to create Calendar event for bill {bill_number}: {exc}")

    logger.info(f"Calendar sync complete: {created} created, {failed} failed.")
    return {"created": created, "failed": failed, "failures": failures}
