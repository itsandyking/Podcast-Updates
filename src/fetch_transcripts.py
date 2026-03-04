"""Fetch web-published transcripts (NPR) as an alternative to local STT."""

from __future__ import annotations

import logging
import re

import httpx

from .config import Show
from .fetch_rss import Episode

logger = logging.getLogger(__name__)


async def fetch_npr_transcript(episode: Episode, show: Show) -> str | None:
    """Try to fetch an NPR transcript for the given episode.

    NPR publishes transcripts at npr.org/transcripts/{episode_id}.
    The episode ID can sometimes be extracted from the RSS entry link or audio URL.
    """
    if not show.web_transcript.enabled or show.web_transcript.parser != "npr":
        return None

    # Try to extract the NPR episode ID from the audio URL
    # NPR audio URLs often contain a numeric ID
    match = re.search(r"/(\d{9,})/", episode.audio_url)
    if not match:
        logger.debug("Could not extract NPR episode ID from audio URL for %s", episode.title)
        return None

    episode_id = match.group(1)
    transcript_url = f"https://www.npr.org/transcripts/{episode_id}"

    logger.info("Checking NPR transcript at %s", transcript_url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        try:
            resp = await client.get(transcript_url)
            if resp.status_code != 200:
                logger.debug("NPR transcript not available (HTTP %d) for %s", resp.status_code, episode.title)
                return None
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch NPR transcript: %s", e)
            return None

    # Extract text from the transcript page
    text = _extract_npr_transcript_text(resp.text)
    if text and len(text) > 200:
        logger.info("Found NPR transcript for %s (%d chars)", episode.title, len(text))
        return text

    logger.debug("NPR transcript page found but content too short for %s", episode.title)
    return None


def _extract_npr_transcript_text(html: str) -> str:
    """Extract plain text from an NPR transcript HTML page.

    NPR transcript pages wrap content in <div class="transcript storytext">
    with paragraphs inside. We do basic HTML stripping without requiring
    a heavy parsing library.
    """
    # Find the transcript div
    match = re.search(
        r'<div[^>]*class="[^"]*transcript[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html,
        re.DOTALL,
    )
    if not match:
        return ""

    content = match.group(1)

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", "\n", content)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


async def try_web_transcript(episode: Episode, show: Show) -> str | None:
    """Attempt to fetch a web transcript for the episode.

    Returns the transcript text if available, None otherwise.
    """
    if show.web_transcript.parser == "npr":
        return await fetch_npr_transcript(episode, show)

    # Other parsers can be added here (NYT, etc.)
    return None
