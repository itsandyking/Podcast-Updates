"""Transcribe podcast audio using Moonshine or faster-whisper."""

from __future__ import annotations

import array
import logging
import struct
import subprocess
import tempfile
import wave
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
        text = _transcribe_moonshine(audio_path)
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


def _mp3_to_wav(mp3_path: Path) -> Path | None:
    """Convert MP3 to 16kHz mono WAV using ffmpeg."""
    wav_path = Path(tempfile.mktemp(suffix=".wav"))
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(mp3_path),
                "-ar", "16000", "-ac", "1", "-f", "wav",
                str(wav_path),
            ],
            capture_output=True,
            check=True,
        )
        return wav_path
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg conversion failed: %s", e.stderr.decode())
        return None


def _transcribe_moonshine(audio_path: Path) -> str | None:
    """Transcribe using Moonshine voice."""
    try:
        from moonshine_voice.download import download_model_from_info, find_model_info
        from moonshine_voice.transcriber import Transcriber
        from moonshine_voice.moonshine_api import ModelArch
    except ImportError:
        logger.error(
            "moonshine-voice not installed. Install with: pip install moonshine-voice"
        )
        return None

    # Convert MP3 to WAV
    logger.info("Converting MP3 to WAV for Moonshine...")
    wav_path = _mp3_to_wav(audio_path)
    if not wav_path:
        return None

    try:
        # Load WAV as PCM float samples using wave module
        with wave.open(str(wav_path), "rb") as wf:
            n_frames = wf.getnframes()
            raw_bytes = wf.readframes(n_frames)
            # Convert 16-bit PCM to float [-1.0, 1.0]
            samples = struct.unpack(f"<{n_frames}h", raw_bytes)
            audio_data = [s / 32768.0 for s in samples]

        logger.info("Audio loaded: %d samples (%.1f seconds)", len(audio_data), len(audio_data) / 16000)

        # Download model if needed and get the correct path
        info = find_model_info("en", ModelArch.BASE)
        model_path, model_arch = download_model_from_info(info)
        transcriber = Transcriber(model_path, model_arch=model_arch)

        # Transcribe
        transcript = transcriber.transcribe_without_streaming(audio_data, sample_rate=16000)

        # Extract text from transcript lines
        lines = []
        for line in transcript.lines:
            lines.append(line.text.strip())

        transcriber.close()

        text = "\n\n".join(line for line in lines if line)
        return text if text else None
    except Exception as e:
        logger.error("Moonshine transcription failed: %s", e)
        return None
    finally:
        wav_path.unlink(missing_ok=True)


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
