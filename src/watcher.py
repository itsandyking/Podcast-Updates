"""Pre-fetch watcher — polls RSS feeds and transcribes new episodes as they drop.

Runs as a background daemon on Pi or Mac. When a new episode is detected it claims
the episode (atomic file lock prevents duplicate work across machines sharing via
Syncthing), downloads the audio, and transcribes to the stable cache path:

    data/transcripts/{show_slug}/{pub_date}.txt

When all expected (non-afternoon, daily) shows for a config have transcripts, the
watcher automatically triggers the analysis pipeline. A deadline from the schedule
acts as a safety net — if the deadline passes with >= 2 transcripts, it triggers
anyway to handle shows that skip a day or publish late.

Mac is preferred for transcription (mlx-whisper is ~40x faster than Pi's CPU-bound
faster-whisper). The Pi runs with --defer to give the Mac a head start. If the Mac
is offline or asleep, the Pi picks up after the defer window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml

from .config import DATA_DIR, PipelineConfig, ROOT_DIR, load_config
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

# Minimum transcripts needed before the deadline safety-net triggers.
_MIN_TRANSCRIPTS_FOR_DEADLINE = 2


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


# ── Trigger logic ────────────────────────────────────────────────────────────


def _trigger_flag_path(config_path: Path, target_date: date) -> Path:
    """Flag file that prevents triggering the same pipeline twice per day."""
    name = config_path.stem  # e.g. "shows" or "shows_tech"
    return DATA_DIR / "watcher" / f"{name}_{target_date.isoformat()}.triggered"


def _already_triggered(config_path: Path, target_date: date) -> bool:
    return _trigger_flag_path(config_path, target_date).exists()


def _mark_triggered(config_path: Path, target_date: date) -> None:
    flag = _trigger_flag_path(config_path, target_date)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


def _expected_shows(config: PipelineConfig, target_date: date) -> list[str]:
    """Return slugs of shows expected to have episodes today (non-afternoon, daily)."""
    weekday = target_date.weekday()  # 0=Mon, 6=Sun
    expected = []
    for show in config.shows:
        if show.afternoon_release:
            continue
        if show.cadence == "weekly":
            continue
        # Daily shows publish Mon-Sat (skip Sunday for news)
        if weekday == 6:  # Sunday
            continue
        expected.append(show.slug)
    return expected


def _ready_transcripts(config: PipelineConfig, target_date: date) -> set[str]:
    """Return slugs of shows that have a transcript for target_date."""
    date_str = target_date.isoformat()
    ready = set()
    for show in config.shows:
        if stable_transcript_path(show.slug, date_str).exists():
            ready.add(show.slug)
    return ready


def _parse_deadline_hour(config_path: Path) -> int | None:
    """Extract the hour from the schedule.cron field in a config YAML.

    Returns the hour as an int, or None if no schedule is configured.
    """
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        cron = raw.get("schedule", {}).get("cron", "")
        if not cron:
            return None
        # cron format: "minute hour day month weekday"
        parts = cron.split()
        return int(parts[1])
    except Exception:
        return None


def _should_trigger(
    config: PipelineConfig,
    config_path: Path,
    target_date: date,
) -> bool:
    """Decide whether to trigger the analysis pipeline.

    Triggers when:
    1. All expected (non-afternoon, daily) shows have transcripts, OR
    2. The deadline hour has passed and we have >= _MIN_TRANSCRIPTS_FOR_DEADLINE transcripts.

    Never triggers if already triggered today for this config.
    """
    if _already_triggered(config_path, target_date):
        return False

    ready = _ready_transcripts(config, target_date)
    if not ready:
        return False

    expected = _expected_shows(config, target_date)

    # Condition 1: all expected shows are ready
    if expected and all(slug in ready for slug in expected):
        missing_afternoon = [
            s.name for s in config.shows
            if s.afternoon_release and s.slug not in ready and s.cadence != "weekly"
        ]
        if missing_afternoon:
            logger.info(
                "Watcher: all expected morning shows ready. Afternoon shows still pending: %s",
                ", ".join(missing_afternoon),
            )
        logger.info(
            "Watcher: all %d expected shows have transcripts — triggering pipeline",
            len(expected),
        )
        return True

    # Condition 2: deadline safety net
    deadline_hour = _parse_deadline_hour(config_path)
    if deadline_hour is not None:
        now = datetime.now()
        if now.hour >= deadline_hour and len(ready) >= _MIN_TRANSCRIPTS_FOR_DEADLINE:
            logger.info(
                "Watcher: deadline %02d:00 passed with %d/%d transcripts — triggering pipeline",
                deadline_hour, len(ready), len(expected) if expected else len(config.shows),
            )
            return True

    return False


async def _trigger_pipeline(config_path: Path, target_date: date) -> None:
    """Run the full pipeline (analysis + email) for a config."""
    from .pipeline import run_pipeline

    config_name = config_path.stem
    logger.info("Watcher: triggering pipeline for %s (%s)", config_name, target_date)
    try:
        result = await run_pipeline(target_date, config_path)
        if result:
            logger.info("Watcher: pipeline complete for %s — %s", config_name, result)
        else:
            logger.warning("Watcher: pipeline returned no result for %s", config_name)
    except Exception as exc:
        logger.error("Watcher: pipeline failed for %s: %s", config_name, exc)


# ── Episode processing ───────────────────────────────────────────────────────


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
    defer_secs: int = 0,
) -> None:
    """Claim, transcribe, and save one episode to the stable cache."""
    pub_date_str = ep.published.date().isoformat()

    # Skip if already transcribed (possibly by the other machine via Syncthing)
    if stable_transcript_path(ep.show_slug, pub_date_str).exists():
        return

    # Defer: give the preferred machine (Mac) time to claim first
    if defer_secs > 0:
        logger.debug("Deferring %s for %ds (letting preferred machine claim first)", ep.show_name, defer_secs)
        await asyncio.sleep(defer_secs)
        # Re-check after waiting — the other machine may have finished
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
        _release_claim(ep.show_slug, pub_date_str)


# ── Main loop ────────────────────────────────────────────────────────────────


async def watch(
    config_paths: list[Path] | None = None,
    interval_secs: int = 900,
    max_concurrent_transcriptions: int = 2,
    defer_secs: int = 0,
) -> None:
    """Poll all configured show feeds and pre-transcribe new episodes.

    After each poll cycle, checks if enough transcripts are ready to trigger
    the analysis pipeline for each config.

    interval_secs: how often to re-poll each config (default 15 min)
    max_concurrent_transcriptions: semaphore cap for CPU-bound transcription
    defer_secs: seconds to wait before claiming episodes (gives preferred machine
                time to claim first; 0 = claim immediately, used on Mac)
    """
    if config_paths is None:
        config_paths = [p for p in _DEFAULT_CONFIG_PATHS if p.exists()]

    if not config_paths:
        logger.error("No config files found — nothing to watch")
        return

    logger.info(
        "Watcher started — polling %d config(s) every %ds, defer %ds",
        len(config_paths), interval_secs, defer_secs,
    )
    transcription_sem = asyncio.Semaphore(max_concurrent_transcriptions)

    while True:
        target_date = datetime.now(timezone.utc).date()

        for config_path in config_paths:
            try:
                config = load_config(config_path)
                episodes = fetch_all_episodes(config.shows, target_date)
                tasks = [
                    _process_episode(ep, config, transcription_sem, defer_secs)
                    for ep in episodes
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                # Check if we should trigger the pipeline
                if _should_trigger(config, config_path, target_date):
                    _mark_triggered(config_path, target_date)
                    await _trigger_pipeline(config_path, target_date)

            except Exception as exc:
                logger.error("Watcher error processing %s: %s", config_path.name, exc)

        logger.debug("Watcher: poll cycle complete — sleeping %ds", interval_secs)
        await asyncio.sleep(interval_secs)


def _default_defer() -> int:
    """Auto-detect defer: 0 on Mac (preferred), 300 on Pi/Linux (deferred)."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return 0
    return 300


def main() -> None:
    """CLI entry point: podcast-watch [--interval N] [--defer N] [config_path ...]"""
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
        "--defer", type=int, default=None,
        help="Seconds to wait before claiming episodes, giving the preferred machine "
             "time to claim first (default: 0 on Mac, 300 on Linux/Pi)",
    )
    parser.add_argument(
        "configs", nargs="*", type=Path,
        help="Config YAML paths to watch (default: all four shows.yaml files)",
    )
    args = parser.parse_args()

    defer = args.defer if args.defer is not None else _default_defer()
    config_paths = args.configs if args.configs else None
    asyncio.run(watch(config_paths, args.interval, args.concurrency, defer))


if __name__ == "__main__":
    main()
