"""Download podcast audio files from RSS enclosure URLs."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .config import DATA_DIR
from .fetch_rss import Episode

logger = logging.getLogger(__name__)

AUDIO_DIR = DATA_DIR / "audio"


async def download_episode(episode: Episode, date_str: str) -> Path | None:
    """Download an episode's audio file.

    Returns the path to the downloaded file, or None on failure.
    """
    dest_dir = AUDIO_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{episode.show_slug}.mp3"

    if dest_path.exists():
        logger.info("Audio already downloaded: %s", dest_path)
        return dest_path

    logger.info("Downloading %s — %s", episode.show_name, episode.title)

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        try:
            async with client.stream("GET", episode.audio_url) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
        except httpx.HTTPError as e:
            logger.error("Failed to download %s: %s", episode.show_name, e)
            dest_path.unlink(missing_ok=True)
            return None

    size_mb = dest_path.stat().st_size / (1024 * 1024)
    logger.info("Downloaded %s (%.1f MB)", dest_path.name, size_mb)
    return dest_path


def cleanup_audio(date_str: str) -> None:
    """Delete downloaded audio files for a given date."""
    audio_dir = AUDIO_DIR / date_str
    if not audio_dir.exists():
        return
    for f in audio_dir.iterdir():
        f.unlink()
        logger.debug("Deleted %s", f)
    audio_dir.rmdir()
    logger.info("Cleaned up audio for %s", date_str)
