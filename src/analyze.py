"""Send transcripts to Google Gemini for cross-show analysis."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from google import genai
from google.genai import types
import jinja2

from .config import PipelineConfig, Show, ROOT_DIR

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3


async def _generate_with_retry(client, model, contents, config) -> str | None:
    """Call generate_content with retry on 429 rate-limit errors."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
            return response.text
        except Exception as e:
            msg = str(e)
            if "429" in msg:
                # Parse retry delay from error message, default to 60s
                m = re.search(r"retryDelay.*?(\d+)s", msg)
                wait = int(m.group(1)) + 5 if m else 60
                if attempt < _RETRY_ATTEMPTS - 1:
                    logger.warning("Rate limited — retrying in %ds (attempt %d/%d)", wait, attempt + 1, _RETRY_ATTEMPTS)
                    await asyncio.sleep(wait)
                    continue
            raise
    return None


def _render_template(path, **kwargs) -> str:
    """Render a Jinja2 template file with the given variables."""
    template_text = (ROOT_DIR / path).read_text()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(template_text).render(**kwargs)


async def _extract_show_summary(
    client: genai.Client,
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
        return await _generate_with_retry(
            client, config.analysis.model, transcript,
            types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=1024),
        )
    except Exception as e:
        logger.error("Gemini API error (extract %s): %s", show.slug, e)
        return None


async def _synthesize(
    client: genai.Client,
    config: PipelineConfig,
    summaries: dict[str, str],
    target_date: date,
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

    # Build user message from per-show summaries
    blocks = []
    for show in config.shows:
        if show.slug in summaries:
            blocks.append(f"### Summary: {show.name}\n\n{summaries[show.slug]}")
    user_message = "\n\n---\n\n".join(blocks)

    try:
        result = await _generate_with_retry(
            client, config.analysis.model, user_message,
            types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=config.analysis.max_tokens),
        )
        logger.info("Analysis complete")
        return result
    except Exception as e:
        logger.error("Gemini API error (synthesize): %s", e)
        return None


async def _single_show_fallback(
    client: genai.Client,
    config: PipelineConfig,
    show: Show,
    text: str,
    target_date: date,
) -> str | None:
    """Produce a simple summary when only one show is available."""
    try:
        return await _generate_with_retry(
            client, config.analysis.model, text,
            types.GenerateContentConfig(
                system_instruction=(
                    f"You are a news analyst. Summarize this podcast episode from {show.name} "
                    f"aired on {target_date.isoformat()}. Identify each story covered, "
                    "the editorial angle, and key facts."
                ),
                max_output_tokens=config.analysis.max_tokens,
            ),
        )
    except Exception as e:
        logger.error("Gemini API error (single-show): %s", e)
        return None


async def analyze_transcripts(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> str | None:
    """Send transcripts to Gemini for cross-show analysis.

    Returns the briefing text, or None on failure.
    """
    client = genai.Client(api_key=config.gemini_api_key)

    if len(transcripts) == 0:
        logger.warning("No transcripts available for analysis")
        return None

    if len(transcripts) == 1:
        logger.warning(
            "Only 1 transcript available — cross-show analysis requires at least 2"
        )
        slug, text = next(iter(transcripts.items()))
        show = next((s for s in config.shows if s.slug == slug), None)
        if show is None:
            logger.error("Unknown show slug: %s", slug)
            return None
        return await _single_show_fallback(client, config, show, text, target_date)

    logger.info(
        "Pass 1: extracting summaries from %d shows via %s",
        len(transcripts),
        config.analysis.model,
    )

    # Build a mapping from slug to Show
    shows_by_slug = {s.slug: s for s in config.shows}

    # Pass 1: extract per-show summaries in parallel, max 4 concurrent
    sem = asyncio.Semaphore(4)

    async def _bounded_extract(slug, text):
        async with sem:
            return await _extract_show_summary(
                client, config, shows_by_slug[slug], text, target_date
            )

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

    logger.info(
        "Pass 1 complete: %d/%d summaries extracted", len(summaries), len(transcripts)
    )

    if not summaries:
        logger.error("All Pass 1 extractions failed")
        return None

    # Pass 2: synthesize summaries into briefing
    logger.info("Pass 2: synthesizing cross-show briefing")
    return await _synthesize(client, config, summaries, target_date)
