"""Main pipeline orchestrator — runs all steps in sequence."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .config import DATA_DIR, load_config
from .download_audio import cleanup_audio, download_episode
from .fetch_rss import fetch_all_episodes
from .fetch_transcripts import try_web_transcript
from .transcribe import transcribe_audio
from .analyze import analyze_transcripts
from .deliver import save_daily_transcripts, deliver_transcripts_email, deliver_email
from .episode_ledger import load_ledger, is_processed, mark_processed

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


async def run_pipeline(target_date: date | None = None, config_path: Path | None = None) -> Path | None:
    """Execute the full daily pipeline.

    Returns the path to the saved briefing, or None on failure.
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc).date()

    date_str = target_date.isoformat()
    setup_logging(target_date)

    logger.info("=== Podcast Updates pipeline starting for %s ===", date_str)

    config = load_config(config_path)

    # Already-ran guard — use group subdirectory when group is set
    from .config import ROOT_DIR
    if config.group:
        combined_path = ROOT_DIR / "daily_transcripts" / date_str / config.group / "all-transcripts.md"
    else:
        combined_path = ROOT_DIR / "daily_transcripts" / date_str / "all-transcripts.md"
    if combined_path.exists():
        logger.info("Transcripts for %s already exist at %s — skipping", date_str, combined_path)
        return combined_path

    # Step 1: Fetch RSS feeds
    logger.info("Step 1: Fetching RSS feeds")
    episodes = fetch_all_episodes(config.shows, target_date)
    if not episodes:
        logger.error("No episodes found from any show — aborting")
        return None
    logger.info("Found %d episodes", len(episodes))

    # Filter out already-processed episodes
    ep_ledger = load_ledger(config.group)
    fresh = [ep for ep in episodes if not is_processed(ep.guid, ep_ledger)]
    skipped = len(episodes) - len(fresh)
    if skipped:
        for ep in episodes:
            if is_processed(ep.guid, ep_ledger):
                logger.info("Skipping %s — episode already processed: %s", ep.show_name, ep.title)
    if not fresh:
        logger.info("All episodes already processed — nothing to do")
        return combined_path if combined_path.exists() else None
    episodes = fresh

    # Step 2 & 3: Get transcripts (web or audio+STT)
    # Group episodes by show — weekly pipelines may have several per show
    transcripts: dict[str, str] = {}
    transcript_sources: dict[str, str] = {}
    shows_by_slug = {s.slug: s for s in config.shows}

    episodes_by_show: dict[str, list] = defaultdict(list)
    for ep in episodes:
        episodes_by_show[ep.show_slug].append(ep)

    for show_slug, show_episodes in episodes_by_show.items():
        show = shows_by_slug.get(show_slug)
        if not show:
            continue

        multi = len(show_episodes) > 1
        collected: list[tuple] = []  # (episode, text)

        for ep in show_episodes:
            # Use a date-scoped key when a show has multiple episodes to avoid filename collisions
            ep_key = f"{show_slug}-{ep.published.strftime('%Y%m%d')}" if multi else show_slug

            logger.info("Step 2: Checking web transcript for %s: %s", ep.show_name, ep.title)
            web_text = await try_web_transcript(ep, show)
            if web_text:
                collected.append((ep, web_text))
                transcript_sources[show_slug] = f"{show.web_transcript.parser}_web"
                logger.info("Using web transcript for %s", ep.title)
                continue

            logger.info("Step 3: Downloading audio for %s: %s", ep.show_name, ep.title)
            audio_path = await download_episode(ep, date_str, ep_key)
            if not audio_path:
                logger.warning("Could not download audio for %s — skipping", ep.title)
                continue

            logger.info("Step 4: Transcribing %s", ep.title)
            text = transcribe_audio(audio_path, show_slug, date_str, config.transcription, ep_key)
            if text:
                collected.append((ep, text))
                transcript_sources[show_slug] = config.transcription.engine
            else:
                logger.warning("Transcription failed for %s", ep.title)

        if not collected:
            continue

        if len(collected) == 1:
            transcripts[show_slug] = collected[0][1]
        else:
            blocks = []
            for ep, text in collected:
                pub = ep.published.strftime("%-d %b %Y")
                blocks.append(f"### \"{ep.title}\" ({pub})\n\n{text}")
            transcripts[show_slug] = "\n\n---\n\n".join(blocks)
            logger.info("Combined %d episodes for %s", len(collected), show.name)

    total_episodes = sum(len(v) for v in episodes_by_show.values())
    logger.info("Transcripts ready: %d/%d shows (%d episodes)", len(transcripts), len(episodes_by_show), total_episodes)

    if not transcripts:
        logger.error("No transcripts available — aborting")
        return None

    # Step 5: Package transcripts
    logger.info("Step 5: Saving transcripts")
    combined_path = save_daily_transcripts(config, transcripts, target_date)

    # Step 6: Analyze and deliver
    # shutil.which respects PATH; fall back to the known install location for cron environments
    claude_bin = shutil.which("claude") or "/home/piking5/.local/bin/claude"
    if Path(claude_bin).exists():
        logger.info("Step 6: Running claude --print analysis")
        briefing = await analyze_transcripts(config, transcripts, target_date)
        if briefing and config.delivery.method == "email":
            logger.info("Step 6b: Emailing briefing")
            if deliver_email(briefing, target_date, episodes):
                mark_processed(episodes, config.group, target_date)
        elif not briefing:
            logger.warning("Analysis failed — falling back to transcript email")
            if config.delivery.method == "email":
                deliver_transcripts_email(combined_path, target_date)
    else:
        logger.info("Step 6: claude CLI not found — emailing transcripts for manual analysis")
        if config.delivery.method == "email":
            deliver_transcripts_email(combined_path, target_date)

    # Step 7: Cleanup
    if config.transcription.cleanup_audio:
        logger.info("Step 7: Cleaning up audio files")
        cleanup_audio(date_str)

    logger.info("=== Pipeline complete — transcripts at %s ===", combined_path)
    return combined_path


async def run_analyze_only(target_date: date | None = None, config_path: Path | None = None) -> bool:
    """Load saved transcripts for target_date and re-run analysis + email delivery."""
    if target_date is None:
        target_date = datetime.now(timezone.utc).date()

    setup_logging(target_date)
    config = load_config(config_path)

    from .config import ROOT_DIR
    if config.group:
        transcript_dir = ROOT_DIR / "daily_transcripts" / target_date.isoformat() / config.group
    else:
        transcript_dir = ROOT_DIR / "daily_transcripts" / target_date.isoformat()

    if not transcript_dir.exists():
        logger.error("No saved transcripts found at %s", transcript_dir)
        return False

    transcripts: dict[str, str] = {}
    for show in config.shows:
        p = transcript_dir / f"{show.slug}.md"
        if p.exists():
            transcripts[show.slug] = p.read_text()
            logger.info("Loaded transcript: %s (%d chars)", show.slug, len(transcripts[show.slug]))

    if not transcripts:
        logger.error("No transcript files found in %s", transcript_dir)
        return False

    # Fetch episodes from RSS to get GUIDs for the ledger
    episodes = fetch_all_episodes(config.shows, target_date)

    logger.info("Running analysis on %d saved transcripts for %s", len(transcripts), target_date)
    briefing = await analyze_transcripts(config, transcripts, target_date)
    if not briefing:
        logger.error("Analysis failed")
        return False

    from .deliver import save_briefing
    save_briefing(config, briefing, target_date, {s: "saved" for s in transcripts})

    if config.delivery.method == "email":
        ok = deliver_email(briefing, target_date)
        if ok and episodes:
            mark_processed(episodes, config.group, target_date)
    return True


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]
    target = None
    config_path = None

    i = 0
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config_path = Path(args[i + 1])
            i += 2
        else:
            target = date.fromisoformat(args[i])
            i += 1

    result = asyncio.run(run_pipeline(target, config_path))
    sys.exit(0 if result else 1)


def main_analyze() -> None:
    """CLI entry point for re-running analysis on already-fetched transcripts."""
    args = sys.argv[1:]
    target = None
    config_path = None

    i = 0
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config_path = Path(args[i + 1])
            i += 2
        else:
            target = date.fromisoformat(args[i])
            i += 1

    result = asyncio.run(run_analyze_only(target, config_path))
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
