"""Send transcripts to Claude for cross-show analysis."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

import jinja2
import yaml

from .config import PipelineConfig, Show, ROOT_DIR, DATA_DIR
from .theme_ledger import load_ledger, save_ledger, prune_stale, format_ledger_for_prompt

logger = logging.getLogger(__name__)

_RATE_LIMIT_PHRASES = ("rate limit", "429", "too many requests", "quota exceeded")


def _load_recent_briefings(target_date: date, group: str = "", history_days: int = 7) -> str:
    """Load briefings from the last history_days days (excluding today) as historical context."""
    briefings_dir = DATA_DIR / "briefings"
    if not briefings_dir.exists():
        return ""

    blocks = []
    for offset in range(1, history_days + 1):
        day = target_date - timedelta(days=offset)
        # Support optional group prefix (e.g. "tech-2026-03-07.md")
        candidates = [
            briefings_dir / f"{day.isoformat()}.md",
            briefings_dir / f"{group}-{day.isoformat()}.md" if group else None,
        ]
        for path in candidates:
            if path and path.exists():
                text = path.read_text().strip()
                # Strip YAML frontmatter
                if text.startswith("---"):
                    end = text.find("---", 3)
                    if end != -1:
                        text = text[end + 3:].strip()
                if text:
                    blocks.append(f"### Briefing from {day.isoformat()}\n\n{text}")
                break

    if not blocks:
        return ""

    header = f"## Recent Coverage ({history_days}-day archive)\n\n"
    return header + "\n\n---\n\n".join(blocks)


async def _generate_with_claude_cli(system: str, user: str, model: str) -> str | None:
    """Call `claude --print` as an async subprocess.

    Returns None on rate-limit (caller should skip to next cron run, not retry).
    """
    claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    cmd = [claude_bin, "--print", "--model", model, "--max-turns", "1", "--system-prompt", system]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=user.encode()), timeout=300)
    except asyncio.TimeoutError:
        logger.error("claude --print timed out after 300s")
        return None

    out = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        err_lower = err.lower()
        if any(phrase in err_lower for phrase in _RATE_LIMIT_PHRASES):
            logger.warning("claude --print rate limited — skipping to next cron run: %s", err)
            return None  # Do not retry; next scheduled run will pick this up
        logger.error("claude --print failed (exit %d): %s", proc.returncode, err)
        return None

    if err:
        logger.debug("claude --print stderr: %s", err)
    return out or None


def _render_template(path, **kwargs) -> str:
    """Render a Jinja2 template file with the given variables."""
    template_text = (ROOT_DIR / path).read_text()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(template_text).render(**kwargs)


async def _extract_show_summary(
    config: PipelineConfig,
    show: Show,
    transcript: str,
    target_date: date,
) -> str | None:
    """Pass 1: extract structured story summary for a single show."""
    system_prompt = _render_template(
        "config/prompt_extract.md",
        show_name=show.name,
        publisher=show.publisher,
        format=show.format,
        duration_min=show.typical_length_min,
        date=target_date.isoformat(),
    )
    try:
        return await _generate_with_claude_cli(system_prompt, transcript, config.analysis.model)
    except Exception as e:
        logger.error("claude --print error (extract %s): %s", show.slug, e)
        return None


async def _synthesize(
    config: PipelineConfig,
    summaries: dict[str, str],
    target_date: date,
    ledger_context: str = "",
    history_context: str = "",
) -> str | None:
    """Pass 2: synthesize per-show summaries into a cross-show briefing."""
    shows_meta = []
    for show in config.shows:
        if show.slug in summaries:
            shows_meta.append(
                {
                    "name": show.name,
                    "publisher": show.publisher,
                    "format": show.format,
                    "duration_min": show.typical_length_min,
                }
            )

    system_prompt = _render_template(
        config.analysis.prompt_file,
        show_count=len(shows_meta),
        date=target_date.isoformat(),
        shows=shows_meta,
    )

    # Build user message: historical briefings → theme ledger → today's summaries
    blocks = []
    if history_context:
        blocks.append(history_context)
    if ledger_context:
        blocks.append(ledger_context)
    for show in config.shows:
        if show.slug in summaries:
            blocks.append(f"### Summary: {show.name}\n\n{summaries[show.slug]}")
    user_message = "\n\n---\n\n".join(blocks)

    model = config.analysis.synthesis_model or config.analysis.model
    try:
        result = await _generate_with_claude_cli(system_prompt, user_message, model)
        logger.info("Analysis complete (model: %s)", model)
        return result
    except Exception as e:
        logger.error("claude --print error (synthesize): %s", e)
        return None


async def _update_ledger(
    config: PipelineConfig,
    briefing: str,
    existing_themes: list[dict],
    target_date: date,
) -> list[dict]:
    """Pass 3: update the theme ledger based on today's briefing."""
    ledger_yaml = yaml.dump(
        {"themes": existing_themes},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ) if existing_themes else "themes: []"

    system_prompt = _render_template(
        "config/prompt_ledger_update.md",
        date=target_date.isoformat(),
    )
    user_message = f"## Today's Briefing\n\n{briefing}\n\n## Current Ledger\n\n```yaml\n{ledger_yaml}\n```"

    try:
        result = await _generate_with_claude_cli(system_prompt, user_message, config.analysis.model)
        if not result:
            return existing_themes

        # Strip code fences if present
        yaml_text = result.strip()
        if yaml_text.startswith("```"):
            yaml_text = re.sub(r'^```[a-z]*\n?', '', yaml_text)
            yaml_text = re.sub(r'\n?```\s*$', '', yaml_text)
        yaml_text = yaml_text.strip()

        # Fix unquoted colons in YAML values (e.g. "Magic: The Gathering")
        # by quoting any value line where a colon appears inside the value portion
        def _fix_yaml_colons(text: str) -> str:
            lines = []
            for line in text.split("\n"):
                # Match "  key: value" where value contains an unquoted colon
                m = re.match(r'^(\s+\w[\w_]*:\s+)(.+)$', line)
                if m and ':' in m.group(2) and not m.group(2).startswith(("'", '"')):
                    lines.append(f"{m.group(1)}'{m.group(2)}'")
                else:
                    lines.append(line)
            return "\n".join(lines)

        yaml_text = _fix_yaml_colons(yaml_text)

        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict) and "themes" in parsed:
            logger.info("Ledger updated: %d themes", len(parsed["themes"] or []))
            return parsed["themes"] or []
        logger.warning("Ledger update returned unexpected structure — keeping existing ledger")
        return existing_themes
    except Exception as e:
        logger.warning("Ledger update failed: %s — keeping existing ledger", e)
        return existing_themes


async def _single_show_fallback(
    config: PipelineConfig,
    show: Show,
    text: str,
    target_date: date,
) -> str | None:
    """Produce a simple summary when only one show is available."""
    system = (
        f"You are a news analyst. Summarize this podcast episode from {show.name} "
        f"aired on {target_date.isoformat()}. Identify each story covered, "
        "the editorial angle, and key facts."
    )
    try:
        return await _generate_with_claude_cli(system, text, config.analysis.model)
    except Exception as e:
        logger.error("claude --print error (single-show): %s", e)
        return None


async def analyze_transcripts(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> str | None:
    """Run cross-show analysis via `claude --print`.

    Returns the briefing text, or None on failure.
    """
    if len(transcripts) == 0:
        logger.warning("No transcripts available for analysis")
        return None

    if len(transcripts) == 1:
        logger.warning("Only 1 transcript available — cross-show analysis requires at least 2")
        slug, text = next(iter(transcripts.items()))
        show = next((s for s in config.shows if s.slug == slug), None)
        if show is None:
            logger.error("Unknown show slug: %s", slug)
            return None
        return await _single_show_fallback(config, show, text, target_date)

    logger.info("Pass 1: extracting summaries from %d shows via %s", len(transcripts), config.analysis.model)

    shows_by_slug = {s.slug: s for s in config.shows}

    # Pass 1: extract per-show summaries in parallel, max 4 concurrent
    sem = asyncio.Semaphore(4)

    async def _bounded_extract(slug, text):
        async with sem:
            return await _extract_show_summary(config, shows_by_slug[slug], text, target_date)

    tasks = [
        _bounded_extract(slug, text)
        for slug, text in transcripts.items()
        if slug in shows_by_slug
    ]
    results = await asyncio.gather(*tasks)

    summaries: dict[str, str] = {}
    for slug, summary in zip(transcripts.keys(), results):
        if summary is not None:
            summaries[slug] = summary

    logger.info("Pass 1 complete: %d/%d summaries extracted", len(summaries), len(transcripts))

    if not summaries:
        logger.error("All Pass 1 extractions failed")
        return None

    # Load theme ledger and build context for Pass 2
    themes = load_ledger(config.group)
    themes = prune_stale(themes, target_date, config.analysis.stale_days)
    ledger_context = format_ledger_for_prompt(themes, target_date)
    if themes:
        logger.info("Loaded %d active themes from ledger", len(themes))

    # Load recent briefings for historical context
    history_context = _load_recent_briefings(target_date, config.group, config.analysis.history_days)
    if history_context:
        logger.info("Loaded historical briefings for context (%d-day window)", config.analysis.history_days)

    # Pass 2: synthesize summaries into briefing
    synthesis_model = config.analysis.synthesis_model or config.analysis.model
    logger.info("Pass 2: synthesizing cross-show briefing via %s", synthesis_model)
    briefing = await _synthesize(config, summaries, target_date, ledger_context, history_context)
    if not briefing:
        return None

    # Pass 3: update theme ledger
    logger.info("Pass 3: updating theme ledger")
    updated_themes = await _update_ledger(config, briefing, themes, target_date)
    save_ledger(config.group, updated_themes)

    return briefing
