"""Send transcripts to Claude API for cross-show analysis."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import anthropic
import jinja2

from .config import AnalysisConfig, PipelineConfig, ROOT_DIR

logger = logging.getLogger(__name__)


def build_prompt(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> str:
    """Assemble the analysis prompt from the template and transcripts."""
    prompt_path = ROOT_DIR / config.analysis.prompt_file
    template_text = prompt_path.read_text()

    # Build show metadata for the template
    shows_with_transcripts = []
    for show in config.shows:
        if show.slug in transcripts:
            shows_with_transcripts.append(
                {
                    "name": show.name,
                    "publisher": show.publisher,
                    "format": show.format,
                    "duration_min": show.typical_length_min,
                }
            )

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    template = env.from_string(template_text)
    system_prompt = template.render(
        show_count=len(shows_with_transcripts),
        date=target_date.isoformat(),
        shows=shows_with_transcripts,
    )

    # Build the user message with all transcripts
    transcript_blocks = []
    for slug, text in transcripts.items():
        show = next((s for s in config.shows if s.slug == slug), None)
        name = show.name if show else slug
        transcript_blocks.append(f"### Transcript: {name}\n\n{text}")

    user_message = "\n\n---\n\n".join(transcript_blocks)

    return system_prompt, user_message


def analyze_transcripts(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> str | None:
    """Send transcripts to Claude for cross-show analysis.

    Returns the briefing text, or None on failure.
    """
    if len(transcripts) < 2:
        logger.warning(
            "Only %d transcript(s) available — cross-show analysis requires at least 2",
            len(transcripts),
        )
        if len(transcripts) == 1:
            return _single_show_summary(config, transcripts, target_date)
        return None

    system_prompt, user_message = build_prompt(config, transcripts, target_date)

    logger.info(
        "Sending %d transcripts to Claude (%s) for analysis",
        len(transcripts),
        config.analysis.model,
    )

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    try:
        message = client.messages.create(
            model=config.analysis.model,
            max_tokens=config.analysis.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        briefing = message.content[0].text
        logger.info(
            "Analysis complete — %d tokens in, %d tokens out",
            message.usage.input_tokens,
            message.usage.output_tokens,
        )
        return briefing
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        return None


def _single_show_summary(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> str | None:
    """Produce a simple summary when only one show is available."""
    slug, text = next(iter(transcripts.items()))
    show = next((s for s in config.shows if s.slug == slug), None)
    name = show.name if show else slug

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    try:
        message = client.messages.create(
            model=config.analysis.model,
            max_tokens=config.analysis.max_tokens,
            system=(
                f"You are a news analyst. Summarize this podcast episode from {name} "
                f"aired on {target_date.isoformat()}. Identify each story covered, "
                "the editorial angle, and key facts."
            ),
            messages=[{"role": "user", "content": text}],
        )
        return message.content[0].text
    except anthropic.APIError as e:
        logger.error("Claude API error (single-show): %s", e)
        return None
