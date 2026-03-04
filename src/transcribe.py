"""Transcribe podcast audio using Moonshine or faster-whisper."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import DATA_DIR, TranscriptionConfig

logger = logging.getLogger(__name__)

TRANSCRIPT_DIR = DATA_DIR / "transcripts"


def transcribe_audio(
    audio_path: Path,
    show_slug: str,
    date_str: str,
    config: TranscriptionConfig,
) -> str | None:
    """Transcribe an audio file and save the transcript.

    Returns the transcript text, or None on failure.
    """
    dest_dir = TRANSCRIPT_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{show_slug}.txt"

    if dest_path.exists():
        logger.info("Transcript already exists: %s", dest_path)
        return dest_path.read_text()

    logger.info("Transcribing %s with %s (%s)", audio_path.name, config.engine, config.model)

    if config.engine == "moonshine":
        text = _transcribe_moonshine(audio_path, config.model)
    elif config.engine == "faster-whisper":
        text = _transcribe_faster_whisper(audio_path, config.model)
    else:
        logger.error("Unknown transcription engine: %s", config.engine)
        return None

    if not text:
        return None

    dest_path.write_text(text)
    logger.info("Saved transcript: %s (%d chars)", dest_path, len(text))
    return text


def _transcribe_moonshine(audio_path: Path, model_name: str) -> str | None:
    """Transcribe using Moonshine."""
    try:
        from moonshine import transcribe as moon_transcribe
    except ImportError:
        logger.error(
            "moonshine-voice not installed. Install with: pip install moonshine-voice"
        )
        return None

    try:
        result = moon_transcribe(str(audio_path), model=model_name)
        if isinstance(result, list):
            return "\n".join(result)
        return str(result)
    except Exception as e:
        logger.error("Moonshine transcription failed: %s", e)
        return None


def _transcribe_faster_whisper(audio_path: Path, model_name: str) -> str | None:
    """Transcribe using faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error(
            "faster-whisper not installed. Install with: pip install faster-whisper"
        )
        return None

    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(audio_path), beam_size=5)

        paragraphs = []
        current = []
        for segment in segments:
            current.append(segment.text.strip())
            # Break into paragraphs roughly every 5 segments
            if len(current) >= 5:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))

        return "\n\n".join(paragraphs)
    except Exception as e:
        logger.error("faster-whisper transcription failed: %s", e)
        return None


def load_transcript(show_slug: str, date_str: str) -> str | None:
    """Load an existing transcript from disk."""
    path = TRANSCRIPT_DIR / date_str / f"{show_slug}.txt"
    if path.exists():
        return path.read_text()
    return None
