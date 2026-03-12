"""Track processed episodes to avoid re-processing across runs."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import yaml

from .config import DATA_DIR

logger = logging.getLogger(__name__)

LEDGER_DIR = DATA_DIR / "episode_ledger"
_PRUNE_DAYS = 30


def _ledger_path(group: str) -> Path:
    return LEDGER_DIR / f"{group or 'news'}.yaml"


def load_ledger(group: str) -> dict[str, dict]:
    """Return {guid: entry} for all stored episodes."""
    path = _ledger_path(group)
    if not path.exists():
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return {e["guid"]: e for e in raw.get("episodes", []) if "guid" in e}


def is_processed(guid: str, ledger: dict[str, dict]) -> bool:
    return guid in ledger


def mark_processed(episodes: list, group: str, target_date: date) -> None:
    """Add episodes to the ledger and prune entries older than 30 days."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger = load_ledger(group)

    for ep in episodes:
        if not ep.guid:
            continue
        ledger[ep.guid] = {
            "guid": ep.guid,
            "show": ep.show_slug,
            "title": ep.title,
            "processed": target_date.isoformat(),
        }

    # Prune old entries
    cutoff = (target_date - timedelta(days=_PRUNE_DAYS)).isoformat()
    ledger = {g: e for g, e in ledger.items() if e.get("processed", "") >= cutoff}

    path = _ledger_path(group)
    with open(path, "w") as f:
        yaml.dump({"episodes": list(ledger.values())}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    logger.info("Episode ledger updated: %d entries → %s", len(ledger), path)
