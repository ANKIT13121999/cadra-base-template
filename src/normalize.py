"""Shared normalisation helpers for the Larkfield data room.

Every consumer (prepare, checks, decision engine) must use these functions so
raw export quirks are handled identically everywhere.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_FORWARD_PREFIX_RE = re.compile(r"^\s*(?:fwd?|fw)\s*:\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SENDER_HEADER = re.compile(
    r"(?im)^\s*>?\s*(?:from|sender)\s*:\s*.*?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)


@dataclass(frozen=True)
class ParsedMoney:
    """A money value parsed from the raw export."""

    amount: Decimal
    rule: str
    raw: str


@dataclass(frozen=True)
class ParsedDate:
    """A date/timestamp normalised to ISO-8601 UTC."""

    value: str
    rule: str
    ambiguous: bool
    candidates: tuple[str, ...]
    raw: str


def normalize_email(value: str | None) -> str:
    """The single email normaliser: trim outer whitespace + lowercase."""
    return (value or "").strip().lower()


def normalize_text(value: str | None) -> str:
    """Collapse whitespace and lowercase, for comparisons/dedupe only."""
    return _WHITESPACE_RE.sub(" ", value or "").strip().lower()


def parse_money_usd(raw: str | None) -> ParsedMoney:
    """Parse ``123.00``, ``$123.00`` or ``USD 123.00`` into a 2dp Decimal USD."""
    if raw is None:
        raise ValueError("money value is missing")
    text = str(raw).strip()
    if not text:
        raise ValueError("money value is blank")

    rule = "plain"
    upper = text.upper()
    if upper.startswith("USD"):
        rule = "usd_prefix"
        text = text[3:].strip()
    if text.startswith("$"):
        rule = "dollar_prefix" if rule == "plain" else f"{rule}+dollar_prefix"
        text = text[1:].strip()
    text = text.replace(",", "").strip()

    try:
        amount = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse money value {raw!r}") from exc
    if amount < 0:
        raise ValueError(f"negative money value {raw!r}")
    return ParsedMoney(amount=amount, rule=rule, raw=str(raw))


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date_utc(value: date) -> str:
    return _iso_utc(datetime(value.year, value.month, value.day, tzinfo=timezone.utc))


def parse_date_utc(raw: str | None) -> ParsedDate:
    """Parse ISO date/datetime or ``DD/MM/YYYY`` into ISO-8601 UTC.

    Slash dates are day-first (the export contains 26/08/2026 and 30/09/2026).
    When both day-first and month-first are valid they are reported as ambiguous
    but still resolved day-first, matching the rest of the export.
    """
    if raw is None:
        raise ValueError("date value is missing")
    text = str(raw).strip()
    if not text:
        raise ValueError("date value is blank")

    if _ISO_DATE_RE.match(text):
        parsed = date.fromisoformat(text)
        return ParsedDate(_date_utc(parsed), "iso_date_utc_midnight", False, (_date_utc(parsed),), str(raw))

    slash = _SLASH_DATE_RE.match(text)
    if slash:
        first, second, year = (int(part) for part in slash.groups())
        day_first: date | None = None
        month_first: date | None = None
        try:
            day_first = date(year, second, first)
        except ValueError:
            pass
        try:
            month_first = date(year, first, second)
        except ValueError:
            pass

        if day_first is None and month_first is None:
            raise ValueError(f"invalid slash date {raw!r}")
        if day_first is not None and month_first is not None and day_first != month_first:
            candidates = tuple(sorted({_date_utc(day_first), _date_utc(month_first)}))
            return ParsedDate(_date_utc(day_first), "day_first_dd_mm_yyyy", True, candidates, str(raw))
        chosen = day_first or month_first
        rule = "day_first_dd_mm_yyyy" if day_first is not None else "month_first_mm_dd_yyyy"
        return ParsedDate(_date_utc(chosen), rule, False, (_date_utc(chosen),), str(raw))

    try:
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"cannot parse date/time {raw!r}") from exc
    naive = parsed_dt.tzinfo is None
    if naive:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    value = _iso_utc(parsed_dt)
    rule = "iso_datetime_assumed_utc" if naive else "iso_datetime_utc"
    return ParsedDate(value, rule, False, (value,), str(raw))


def is_forward_subject(subject: str | None) -> bool:
    return bool(_FORWARD_PREFIX_RE.match(subject or ""))


def strip_forward_prefix(subject: str | None) -> str:
    text = subject or ""
    while _FORWARD_PREFIX_RE.match(text):
        text = _FORWARD_PREFIX_RE.sub("", text, count=1)
    return text.strip()


def extract_original_sender(body: str | None, fallback_email: str | None) -> tuple[str, str]:
    """Return ``(normalised_email, source)`` for a forwarded email.

    Preference order: a quoted ``From:``/``Sender:`` header in the body, then
    the outer ``from`` field (the seed forwards preserve the original sender).
    """
    match = _SENDER_HEADER.search(body or "")
    if match:
        return normalize_email(match.group(1)), "quoted_header"
    return normalize_email(fallback_email), "from_field_fallback"


def ticket_dedupe_key(original_from: str, original_subject: str, body: str | None) -> str:
    """Stable key for spotting forwards/re-sends of the same customer email."""
    payload = "\x1f".join(
        (
            normalize_email(original_from),
            normalize_text(original_subject),
            normalize_text(body),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_order_ref(text: str | None) -> str | None:
    """Return ``LF-######`` from any export/email spelling, else ``None``."""
    match = re.search(r"(?:LF[-\s]?)?(\d{6})", text or "", re.IGNORECASE)
    return f"LF-{match.group(1)}" if match else None


def find_emails(text: str | None) -> list[str]:
    return [normalize_email(match) for match in _EMAIL_IN_TEXT.findall(text or "")]


def stable_json_hash(values: Iterable[str]) -> str:
    joined = "\x1f".join(values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
