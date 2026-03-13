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
from .transcribe import load_transcript

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

    # Step 2–4: Fetch transcripts for all episodes in parallel.
    # Downloads are I/O-bound and run fully concurrently.
    # Transcription is CPU-bound; a semaphore caps it at 2 concurrent jobs
    # to use the Pi's 4 cores efficiently without thrashing.
    shows_by_slug = {s.slug: s for s in config.shows}

    episodes_by_show: dict[str, list] = defaultdict(list)
    for ep in episodes:
        episodes_by_show[ep.show_slug].append(ep)

    # mlx-whisper uses the Metal GPU and crashes with concurrent access;
    # faster-whisper/moonshine are CPU-bound and benefit from parallelism.
    max_concurrent = 1 if config.transcription.engine == "mlx-whisper" else 2
    transcription_sem = asyncio.Semaphore(max_concurrent)

    async def _fetch_transcript(ep, show, ep_key):
        """Download + transcribe one episode. Returns (episode, text, source)."""
        pub_date_str = ep.published.date().isoformat()

        # 1. Check stable transcript cache (pre-transcribed by watcher or other machine)
        cached = load_transcript(ep.show_slug, pub_date_str)
        if cached:
            logger.info("Transcript cache hit: %s — %s", ep.show_name, ep.title)
            return ep, cached, "cached"

        # 2. Web transcript (NPR, etc.)
        web_text = await try_web_transcript(ep, show)
        if web_text:
            logger.info("Web transcript: %s — %s", ep.show_name, ep.title)
            return ep, web_text, f"{show.web_transcript.parser}_web"

        # 3. podcast:transcript RSS tag (pre-made, best quality)
        if ep.transcript_url:
            logger.info("Fetching podcast:transcript: %s — %s", ep.show_name, ep.title)
            try:
                import httpx
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                    resp = await client.get(ep.transcript_url)
                    if resp.status_code == 200:
                        return ep, resp.text, "podcast_transcript"
            except Exception as exc:
                logger.warning("podcast:transcript fetch failed for %s: %s", ep.title, exc)

        # 4. Download audio + transcribe
        logger.info("Downloading: %s — %s", ep.show_name, ep.title)
        audio_path = await download_episode(ep, date_str, ep_key)
        if not audio_path:
            logger.warning("Download failed: %s — skipping", ep.title)
            return ep, None, None

        logger.info("Transcribing: %s — %s", ep.show_name, ep.title)
        async with transcription_sem:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(
                None, transcribe_audio,
                audio_path, ep.show_slug, pub_date_str, config.transcription,
            )
        if text:
            return ep, text, config.transcription.engine
        logger.warning("Transcription failed: %s", ep.title)
        return ep, None, None

    # Build one task per episode across all shows
    tasks = []
    for show_slug, show_episodes in episodes_by_show.items():
        show = shows_by_slug.get(show_slug)
        if not show:
            continue
        multi = len(show_episodes) > 1
        for ep in show_episodes:
            ep_key = f"{show_slug}-{ep.published.strftime('%Y%m%d')}" if multi else show_slug
            tasks.append(_fetch_transcript(ep, show, ep_key))

    results = await asyncio.gather(*tasks)

    # Regroup by show, sort oldest-first, concatenate multi-episode blocks
    transcripts: dict[str, str] = {}
    transcript_sources: dict[str, str] = {}
    collected_by_show: dict[str, list] = defaultdict(list)
    for ep, text, source in results:
        if text:
            collected_by_show[ep.show_slug].append((ep, text))
            if source:
                transcript_sources[ep.show_slug] = source

    for show_slug, collected in collected_by_show.items():
        collected.sort(key=lambda x: x[0].published)
        if len(collected) == 1:
            transcripts[show_slug] = collected[0][1]
        else:
            blocks = [
                f"### \"{ep.title}\" ({ep.published.strftime('%-d %b %Y')})\n\n{text}"
                for ep, text in collected
            ]
            transcripts[show_slug] = "\n\n---\n\n".join(blocks)
            logger.info("Combined %d episodes for %s", len(collected), show_slug)

    total_episodes = sum(len(v) for v in episodes_by_show.values())
    logger.info("Transcripts ready: %d/%d shows (%d episodes)", len(transcripts), len(episodes_by_show), total_episodes)

    if not transcripts:
        logger.error("No transcripts available — aborting")
        return None

    # Step 5: Package transcripts
    logger.info("Step 5: Saving transcripts")
    combined_path = save_daily_transcripts(config, transcripts, target_date)

    # Step 6: Analyze and deliver
    # shutil.which respects PATH; fall back to known install locations for cron environments
    # (Pi: ~/.local/bin/claude, Mac: ~/.local/bin/claude or /usr/local/bin/claude)
    import os as _os
    _fallback_bins = [
        "/home/piking5/.local/bin/claude",
        f"/Users/{_os.environ.get('USER', '')}/.local/bin/claude",
        "/usr/local/bin/claude",
    ]
    claude_bin = shutil.which("claude") or next(
        (p for p in _fallback_bins if Path(p).exists()), None
    )
    if claude_bin and Path(claude_bin).exists():
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
