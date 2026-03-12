"""Parse podcast RSS feeds and extract episode metadata + audio URLs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import feedparser

from .config import Show

logger = logging.getLogger(__name__)


@dataclass
class Episode:
    show_slug: str
    show_name: str
    title: str
    published: datetime
    audio_url: str
    duration_seconds: int | None = None
    description: str = ""
    link: str = ""
    guid: str = ""


def fetch_latest_episode(show: Show, target_date: date) -> Episode | None:
    """Fetch the most recent episode from a show's RSS feed.

    Returns None if the feed can't be parsed, has no episodes, or the latest
    episode is older than one day before target_date.
    """
    if not show.rss_url:
        logger.warning("No RSS URL configured for %s — skipping", show.name)
        return None

    logger.info("Fetching RSS feed for %s", show.name)
    feed = feedparser.parse(
        show.rss_url,
        agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )

    if feed.bozo and not feed.entries:
        logger.error("Failed to parse feed for %s: %s", show.name, feed.bozo_exception)
        return None

    if not feed.entries:
        logger.warning("No entries in feed for %s", show.name)
        return None

    entry = feed.entries[0]

    # Extract audio URL from enclosures
    audio_url = ""
    for enclosure in entry.get("enclosures", []):
        if enclosure.get("type", "").startswith("audio/"):
            audio_url = enclosure["href"]
            break

    if not audio_url:
        # Try media:content as fallback
        for media in entry.get("media_content", []):
            if media.get("type", "").startswith("audio/"):
                audio_url = media["url"]
                break

    if not audio_url:
        logger.warning("No audio URL found for %s episode: %s", show.name, entry.get("title"))
        return None

    # Parse publish date
    published = datetime.now(timezone.utc)
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    # Parse duration (itunes:duration can be seconds or HH:MM:SS)
    duration = None
    raw_duration = entry.get("itunes_duration", "")
    if raw_duration:
        duration = _parse_duration(raw_duration)

    episode = Episode(
        show_slug=show.slug,
        show_name=show.name,
        title=entry.get("title", "Unknown"),
        published=published,
        audio_url=audio_url,
        duration_seconds=duration,
        description=entry.get("summary", ""),
        link=entry.get("link", ""),
        guid=entry.get("id", entry.get("link", "")),
    )

    # Date filtering: skip episodes older than the cadence window
    if show.cadence == "weekly":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    if episode.published < cutoff:
        logger.warning(
            "Skipping %s — latest episode (%s) is older than %s",
            show.name,
            episode.published.date().isoformat(),
            cutoff.date().isoformat(),
        )
        return None

    if episode.published.date() < target_date and not show.afternoon_release and show.cadence != "weekly":
        logger.warning(
            "Skipping %s — latest episode (%s) is from a prior day, not today",
            show.name,
            episode.published.date().isoformat(),
        )
        return None

    return episode


def _parse_duration(raw: str) -> int | None:
    """Parse iTunes duration — can be '1234' (seconds) or '20:15' or '1:20:15'."""
    try:
        if ":" not in raw:
            return int(raw)
        parts = raw.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        pass
    return None


def fetch_all_episodes(shows: list[Show], target_date: date) -> list[Episode]:
    """Fetch the latest episode from each configured show."""
    episodes = []
    for show in shows:
        ep = fetch_latest_episode(show, target_date)
        if ep:
            episodes.append(ep)
        else:
            logger.warning("Skipped %s — no episode available", show.name)
    return episodes
