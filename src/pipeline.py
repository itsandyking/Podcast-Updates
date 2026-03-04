"""Main pipeline orchestrator — runs all steps in sequence."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .config import DATA_DIR, load_config
from .download_audio import cleanup_audio, download_episode
from .fetch_rss import fetch_all_episodes
from .fetch_transcripts import try_web_transcript
from .transcribe import transcribe_audio
from .analyze import analyze_transcripts
from .deliver import deliver

logger = logging.getLogger(__name__)

LOG_DIR = DATA_DIR / "logs"


def setup_logging(target_date: date) -> None:
    """Configure logging to both console and daily log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{target_date.isoformat()}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )


async def run_pipeline(target_date: date | None = None) -> Path | None:
    """Execute the full daily pipeline.

    Returns the path to the saved briefing, or None on failure.
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc).date()

    date_str = target_date.isoformat()
    setup_logging(target_date)

    logger.info("=== Podcast Updates pipeline starting for %s ===", date_str)

    config = load_config()
    if not config.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot run analysis")
        return None

    # Step 1: Fetch RSS feeds
    logger.info("Step 1: Fetching RSS feeds")
    episodes = fetch_all_episodes(config.shows)
    if not episodes:
        logger.error("No episodes found from any show — aborting")
        return None
    logger.info("Found %d episodes", len(episodes))

    # Step 2 & 3: Get transcripts (web or audio+STT)
    transcripts: dict[str, str] = {}
    transcript_sources: dict[str, str] = {}
    shows_by_slug = {s.slug: s for s in config.shows}

    for episode in episodes:
        show = shows_by_slug.get(episode.show_slug)
        if not show:
            continue

        # Try web transcript first
        logger.info("Step 2: Checking web transcript for %s", episode.show_name)
        web_text = await try_web_transcript(episode, show)
        if web_text:
            transcripts[episode.show_slug] = web_text
            transcript_sources[episode.show_slug] = f"{show.web_transcript.parser}_web"
            logger.info("Using web transcript for %s", episode.show_name)
            continue

        # Fall back to audio download + local transcription
        logger.info("Step 3: Downloading audio for %s", episode.show_name)
        audio_path = await download_episode(episode, date_str)
        if not audio_path:
            logger.warning("Could not download audio for %s — skipping", episode.show_name)
            continue

        logger.info("Step 4: Transcribing %s", episode.show_name)
        text = transcribe_audio(audio_path, episode.show_slug, date_str, config.transcription)
        if text:
            transcripts[episode.show_slug] = text
            transcript_sources[episode.show_slug] = config.transcription.engine
        else:
            logger.warning("Transcription failed for %s", episode.show_name)

    logger.info("Transcripts ready: %d/%d shows", len(transcripts), len(episodes))

    if not transcripts:
        logger.error("No transcripts available — aborting")
        return None

    # Step 5: Analyze with Claude
    logger.info("Step 5: Running cross-show analysis")
    briefing = analyze_transcripts(config, transcripts, target_date)
    if not briefing:
        logger.error("Analysis failed — aborting")
        return None

    # Step 6: Deliver
    logger.info("Step 6: Delivering briefing")
    path = deliver(config, briefing, target_date, transcript_sources)

    # Step 7: Cleanup
    if config.transcription.cleanup_audio:
        logger.info("Step 7: Cleaning up audio files")
        cleanup_audio(date_str)

    logger.info("=== Pipeline complete — briefing at %s ===", path)
    return path


def main() -> None:
    """CLI entry point."""
    target = None
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    result = asyncio.run(run_pipeline(target))
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
