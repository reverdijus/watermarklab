"""Identifiers and timestamps (ARCHITECTURE.md §1.3)."""

from __future__ import annotations

import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone

SLUG_MAX_LEN = 32


def slugify(text: str, max_len: int = SLUG_MAX_LEN, fallback: str = "run") -> str:
    """Return a ``[a-z0-9-]`` slug of at most ``max_len`` characters."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or fallback


def _stamp(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    return moment.strftime("%Y%m%d-%H%M%S")


def _hex4() -> str:
    return secrets.token_hex(2)


def new_run_id(slug: str) -> str:
    """Return ``YYYYMMDD-HHMMSS-<slug>-<4hex>`` (UTC)."""
    return f"{_stamp()}-{slugify(slug)}-{_hex4()}"


def new_study_id(slug: str) -> str:
    """Return a study id with the same format as run ids (slug = HPO plan name)."""
    return f"{_stamp()}-{slugify(slug, fallback='study')}-{_hex4()}"


def new_job_id() -> str:
    """Return ``job-YYYYMMDD-HHMMSS-<4hex>`` (UTC)."""
    return f"job-{_stamp()}-{_hex4()}"


def utc_iso(epoch: float | None = None) -> str:
    """Format an epoch (default: now) as ISO 8601 UTC with ``Z`` (second precision)."""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    """Return the current time as ISO 8601 UTC with ``Z``."""
    return utc_iso()


def parse_iso(value: str | None) -> float | None:
    """Parse an ISO 8601 timestamp (``Z`` or offset; naive = UTC) into epoch seconds."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()
