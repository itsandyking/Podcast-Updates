"""Send transcripts to Google Gemini for cross-show analysis."""

from __future__ import annotations

import logging
from datetime import date

import google.generativeai as genai
import jinja2

from .config import PipelineConfig, ROOT_DIR

logger = logging.getLogger(__name__)


def build_prompt(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> tuple[str, str]:
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
    """Send transcripts to Gemini for cross-show analysis.

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
        "Sending %d transcripts to Gemini (%s) for analysis",
        len(transcripts),
        config.analysis.model,
    )

    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(
        model_name=config.analysis.model,
        system_instruction=system_prompt,
    )

    try:
        response = model.generate_content(
            user_message,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=config.analysis.max_tokens,
            ),
        )
        briefing = response.text
        logger.info("Analysis complete")
        return briefing
    except Exception as e:
        logger.error("Gemini API error: %s", e)
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

    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(
        model_name=config.analysis.model,
        system_instruction=(
            f"You are a news analyst. Summarize this podcast episode from {name} "
            f"aired on {target_date.isoformat()}. Identify each story covered, "
            "the editorial angle, and key facts."
        ),
    )

    try:
        response = model.generate_content(
            text,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=config.analysis.max_tokens,
            ),
        )
        return response.text
    except Exception as e:
        logger.error("Gemini API error (single-show): %s", e)
        return None
