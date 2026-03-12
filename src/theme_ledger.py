"""Manage the rolling theme ledger for cross-day story continuity."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import yaml

from .config import DATA_DIR

logger = logging.getLogger(__name__)

LEDGER_DIR = DATA_DIR / "themes"


def _ledger_path(group: str) -> Path:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    name = group if group else "default"
    return LEDGER_DIR / f"{name}.yaml"


def load_ledger(group: str) -> list[dict]:
    """Load the theme ledger, returning an empty list if it doesn't exist."""
    path = _ledger_path(group)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return data.get("themes", [])
    except Exception as e:
        logger.warning("Could not load theme ledger: %s", e)
        return []


def save_ledger(group: str, themes: list[dict]) -> None:
    """Write the updated theme ledger to disk."""
    path = _ledger_path(group)
    path.write_text(
        yaml.dump(
            {"themes": themes},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    )
    logger.info("Theme ledger saved: %d themes → %s", len(themes), path)


def prune_stale(themes: list[dict], today: date, stale_days: int = 14) -> list[dict]:
    """Remove themes not seen in stale_days days."""
    cutoff = today - timedelta(days=stale_days)
    kept = []
    for t in themes:
        last_seen = t.get("last_seen")
        try:
            if isinstance(last_seen, str):
                last_seen = date.fromisoformat(last_seen)
            if last_seen and last_seen < cutoff:
                logger.info("Pruning stale theme: %s (last seen %s)", t.get("headline"), last_seen)
                continue
        except (ValueError, TypeError):
            pass
        kept.append(t)
    return kept


def format_ledger_for_prompt(themes: list[dict], today: date) -> str:
    """Format the ledger as compact context for the synthesis prompt."""
    if not themes:
        return ""
    lines = ["## Active Story Threads (prior days)\n"]
    for t in themes:
        last_seen = t.get("last_seen", "?")
        first_seen = t.get("first_seen", "?")
        appearances = t.get("appearances", 1)
        try:
            days_ago = (today - date.fromisoformat(str(last_seen))).days
            recency = f"last seen {days_ago}d ago"
        except (ValueError, TypeError):
            recency = f"last seen {last_seen}"
        lines.append(
            f"- **{t.get('headline', 'Unknown')}** "
            f"(first: {first_seen}, {recency}, {appearances} appearance{'s' if appearances != 1 else ''})\n"
            f"  {t.get('summary', '')}"
        )
    return "\n".join(lines)
