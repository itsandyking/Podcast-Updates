"""Pre-fetch watcher — polls RSS feeds and transcribes new episodes as they drop.

Runs as a background daemon on Pi or Mac. When a new episode is detected it claims
the episode (atomic file lock prevents duplicate work across machines sharing via
Syncthing), downloads the audio, and transcribes to the stable cache path:

    data/transcripts/{show_slug}/{pub_date}.txt

The main pipeline then finds the transcript in cache and skips download/transcription.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import DATA_DIR, ROOT_DIR, load_config
from .download_audio import download_episode
from .fetch_rss import Episode, fetch_all_episodes
from .transcribe import TRANSCRIPT_DIR, stable_transcript_path, transcribe_audio

logger = logging.getLogger(__name__)

# A claim file older than this is assumed stale (crashed process) and retried.
_CLAIM_TTL_SECS = 1800  # 30 minutes

_DEFAULT_CONFIG_PATHS = [
    ROOT_DIR / "config" / "shows.yaml",
    ROOT_DIR / "config" / "shows_tech.yaml",
    ROOT_DIR / "config" / "shows_finance.yaml",
    ROOT_DIR / "config" / "shows_parenting.yaml",
]


def _claim_path(show_slug: str, pub_date_str: str) -> Path:
    return TRANSCRIPT_DIR / show_slug / f"{pub_date_str}.claim"


def _try_claim(show_slug: str, pub_date_str: str) -> bool:
    """Attempt to atomically claim an episode for transcription.

    Returns True if the claim was acquired, False if another process holds it.
    Stale claims (older than _CLAIM_TTL_SECS) are cleared and re-attempted.
    """
    claim = _claim_path(show_slug, pub_date_str)
    claim.parent.mkdir(parents=True, exist_ok=True)

    # Clear stale claim from a crashed process
    if claim.exists():
        age = datetime.now().timestamp() - claim.stat().st_mtime
        if age >= _CLAIM_TTL_SECS:
            logger.info("Clearing stale claim for %s/%s (age %.0fs)", show_slug, pub_date_str, age)
            claim.unlink(missing_ok=True)
        else:
            return False  # Another process holds a fresh claim

    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False  # Race: another machine created the claim between our check and open


def _release_claim(show_slug: str, pub_date_str: str) -> None:
    _claim_path(show_slug, pub_date_str).unlink(missing_ok=True)


async def _fetch_podcast_transcript(url: str) -> str | None:
    """Fetch a pre-made transcript from a podcast:transcript RSS URL."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        logger.warning("Failed to fetch podcast:transcript from %s: %s", url, exc)
    return None


async def _process_episode(
    ep: Episode,
    config,
    transcription_sem: asyncio.Semaphore,
) -> None:
    """Claim, transcribe, and save one episode to the stable cache."""
    pub_date_str = ep.published.date().isoformat()

    # Skip if already transcribed (possibly by the other machine via Syncthing)
    if stable_transcript_path(ep.show_slug, pub_date_str).exists():
        return

    if not _try_claim(ep.show_slug, pub_date_str):
        logger.debug("Skipping %s %s — already claimed", ep.show_name, pub_date_str)
        return

    logger.info("Watcher: processing %s — %s (%s)", ep.show_name, ep.title, pub_date_str)
    text = None
    try:
        # Try podcast:transcript tag first (best quality, no transcription needed)
        if ep.transcript_url:
            logger.info("Watcher: fetching podcast:transcript for %s", ep.title)
            text = await _fetch_podcast_transcript(ep.transcript_url)
            if text:
                dest = stable_transcript_path(ep.show_slug, pub_date_str)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
                logger.info("Watcher: saved podcast:transcript for %s (%d chars)", ep.title, len(text))
                return

        # Download audio
        date_str = datetime.now(timezone.utc).date().isoformat()
        audio_path = await download_episode(ep, date_str)
        if not audio_path:
            logger.warning("Watcher: download failed for %s — skipping", ep.title)
            return

        # Transcribe (CPU-bound, honour semaphore)
        async with transcription_sem:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(
                None, transcribe_audio,
                audio_path, ep.show_slug, pub_date_str, config.transcription,
            )

        if text:
            logger.info("Watcher: transcribed %s — %s (%d chars)", ep.show_name, ep.title, len(text))
        else:
            logger.warning("Watcher: transcription returned nothing for %s", ep.title)
    finally:
        # Release claim whether we succeeded or failed
        if not text:
            _release_claim(ep.show_slug, pub_date_str)
        # If text was written, the transcript file IS the permanent record; keep claim deleted.
        else:
            _release_claim(ep.show_slug, pub_date_str)


async def watch(
    config_paths: list[Path] | None = None,
    interval_secs: int = 900,
    max_concurrent_transcriptions: int = 2,
) -> None:
    """Poll all configured show feeds and pre-transcribe new episodes.

    interval_secs: how often to re-poll each config (default 15 min)
    max_concurrent_transcriptions: semaphore cap for CPU-bound transcription
    """
    if config_paths is None:
        config_paths = [p for p in _DEFAULT_CONFIG_PATHS if p.exists()]

    if not config_paths:
        logger.error("No config files found — nothing to watch")
        return

    logger.info(
        "Watcher started — polling %d config(s) every %ds",
        len(config_paths), interval_secs,
    )
    transcription_sem = asyncio.Semaphore(max_concurrent_transcriptions)

    while True:
        target_date = datetime.now(timezone.utc).date()

        for config_path in config_paths:
            try:
                config = load_config(config_path)
                episodes = fetch_all_episodes(config.shows, target_date)
                tasks = [
                    _process_episode(ep, config, transcription_sem)
                    for ep in episodes
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as exc:
                logger.error("Watcher error processing %s: %s", config_path.name, exc)

        logger.debug("Watcher: poll cycle complete — sleeping %ds", interval_secs)
        await asyncio.sleep(interval_secs)


def main() -> None:
    """CLI entry point: podcast-watch [--interval N] [config_path ...]"""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Pre-fetch watcher: transcribes podcast episodes as they drop"
    )
    parser.add_argument(
        "--interval", type=int, default=900,
        help="Poll interval in seconds (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=2,
        help="Max concurrent transcriptions (default: 2)",
    )
    parser.add_argument(
        "configs", nargs="*", type=Path,
        help="Config YAML paths to watch (default: all four shows.yaml files)",
    )
    args = parser.parse_args()

    config_paths = args.configs if args.configs else None
    asyncio.run(watch(config_paths, args.interval, args.concurrency))


if __name__ == "__main__":
    main()
