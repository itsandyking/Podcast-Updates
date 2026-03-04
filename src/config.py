"""Load and validate pipeline configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"


@dataclass
class WebTranscript:
    enabled: bool = False
    base_url: str = ""
    parser: str = ""


@dataclass
class Show:
    slug: str
    name: str
    publisher: str
    rss_url: str | None
    format: str
    typical_length_min: int
    web_transcript: WebTranscript = field(default_factory=WebTranscript)


@dataclass
class TranscriptionConfig:
    engine: str = "moonshine"
    model: str = "medium"
    cleanup_audio: bool = True


@dataclass
class AnalysisConfig:
    provider: str = "gemini"
    model: str = "gemini-2.0-flash"
    prompt_file: str = "config/prompt.md"
    max_tokens: int = 4096


@dataclass
class DeliveryConfig:
    method: str = "file"
    output_dir: str = "data/briefings"


@dataclass
class PipelineConfig:
    shows: list[Show]
    transcription: TranscriptionConfig
    analysis: AnalysisConfig
    delivery: DeliveryConfig
    gemini_api_key: str = ""


def load_config(config_path: Path | None = None) -> PipelineConfig:
    """Load pipeline configuration from shows.yaml and environment variables."""
    load_dotenv(CONFIG_DIR / ".env")

    if config_path is None:
        config_path = CONFIG_DIR / "shows.yaml"

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    shows = []
    for s in raw["shows"]:
        wt_raw = s.get("web_transcript", {})
        wt = WebTranscript(
            enabled=wt_raw.get("enabled", False),
            base_url=wt_raw.get("base_url", ""),
            parser=wt_raw.get("parser", ""),
        )
        shows.append(
            Show(
                slug=s["slug"],
                name=s["name"],
                publisher=s["publisher"],
                rss_url=s.get("rss_url"),
                format=s["format"],
                typical_length_min=s["typical_length_min"],
                web_transcript=wt,
            )
        )

    transcription = TranscriptionConfig(**raw.get("transcription", {}))
    analysis = AnalysisConfig(**raw.get("analysis", {}))
    delivery = DeliveryConfig(**raw.get("delivery", {}))

    return PipelineConfig(
        shows=shows,
        transcription=transcription,
        analysis=analysis,
        delivery=delivery,
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
    )
