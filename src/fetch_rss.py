"""Parse podcast RSS feeds and extract episode metadata + audio URLs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import feedparser

from .config import Show

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


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


def _parse_entry(entry, show: Show) -> Episode | None:
    """Parse a feed entry into an Episode. Returns None if no audio URL found."""
    audio_url = ""
    for enclosure in entry.get("enclosures", []):
        if enclosure.get("type", "").startswith("audio/"):
            audio_url = enclosure["href"]
            break

    if not audio_url:
        for media in entry.get("media_content", []):
            if media.get("type", "").startswith("audio/"):
                audio_url = media["url"]
                break

    if not audio_url:
        logger.warning("No audio URL found for %s episode: %s", show.name, entry.get("title"))
        return None

    published = datetime.now(timezone.utc)
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    duration = None
    raw_duration = entry.get("itunes_duration", "")
    if raw_duration:
        duration = _parse_duration(raw_duration)

    return Episode(
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


def fetch_recent_episodes(show: Show, target_date: date) -> list[Episode]:
    """Fetch recent episodes from a show's RSS feed within its cadence window.

    For daily shows: returns the single most recent episode published today
    (or yesterday for afternoon_release shows).
    For weekly shows: returns all episodes published within the last 7 days,
    sorted oldest-first so transcripts read chronologically.
    """
    if not show.rss_url:
        logger.warning("No RSS URL configured for %s — skipping", show.name)
        return []

    logger.info("Fetching RSS feed for %s", show.name)
    feed = feedparser.parse(show.rss_url, agent=_USER_AGENT)

    if feed.bozo and not feed.entries:
        logger.error("Failed to parse feed for %s: %s", show.name, feed.bozo_exception)
        return []

    if not feed.entries:
        logger.warning("No entries in feed for %s", show.name)
        return []

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=7)
        if show.cadence == "weekly"
        else datetime.now(timezone.utc) - timedelta(hours=24)
    )

    episodes: list[Episode] = []
    for entry in feed.entries:
        episode = _parse_entry(entry, show)
        if episode is None:
            continue

        # Feeds are reverse-chronological; once we pass the cutoff there's nothing newer
        if episode.published < cutoff:
            break

        if show.cadence == "weekly":
            episodes.append(episode)
        else:
            # Daily: accept only today's episode (or yesterday's for afternoon_release)
            if episode.published.date() < target_date and not show.afternoon_release:
                break
            episodes.append(episode)
            break  # Daily shows: one episode per run

    if not episodes:
        logger.warning("No recent episodes found for %s", show.name)
    elif len(episodes) > 1:
        logger.info("Found %d episodes for %s within 7-day window", len(episodes), show.name)
        episodes.sort(key=lambda e: e.published)  # oldest first → chronological transcripts

    return episodes


def fetch_all_episodes(shows: list[Show], target_date: date) -> list[Episode]:
    """Fetch recent episodes from each configured show."""
    episodes = []
    for show in shows:
        found = fetch_recent_episodes(show, target_date)
        if found:
            episodes.extend(found)
        else:
            logger.warning("Skipped %s — no recent episodes available", show.name)
    return episodes


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
