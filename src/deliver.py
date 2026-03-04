"""Format and deliver the daily briefing."""

from __future__ import annotations

import logging
import smtplib
import os
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from .config import DATA_DIR, DeliveryConfig, PipelineConfig

logger = logging.getLogger(__name__)

BRIEFINGS_DIR = DATA_DIR / "briefings"


def build_frontmatter(
    config: PipelineConfig,
    target_date: date,
    transcript_sources: dict[str, str],
) -> str:
    """Build YAML frontmatter for the briefing file."""
    lines = [
        "---",
        f"date: {target_date.isoformat()}",
        "shows_analyzed:",
    ]
    for show in config.shows:
        if show.slug in transcript_sources:
            lines.extend([
                f"  - name: {show.name}",
                f"    episode_date: {target_date.isoformat()}",
                f"    duration_min: {show.typical_length_min}",
                f"    transcript_source: {transcript_sources[show.slug]}",
            ])

    lines.extend([
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"model: {config.analysis.model}",
        "---",
    ])
    return "\n".join(lines)


def save_briefing(
    config: PipelineConfig,
    briefing_text: str,
    target_date: date,
    transcript_sources: dict[str, str],
) -> Path:
    """Save the briefing as a Markdown file with frontmatter."""
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BRIEFINGS_DIR / f"{target_date.isoformat()}.md"

    frontmatter = build_frontmatter(config, target_date, transcript_sources)
    content = f"{frontmatter}\n\n{briefing_text}\n"

    dest.write_text(content)
    logger.info("Briefing saved to %s", dest)
    return dest


def deliver_email(briefing_text: str, target_date: date) -> bool:
    """Send the briefing via email."""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    email_to = os.environ.get("EMAIL_TO", "")

    if not all([smtp_user, smtp_pass, email_to]):
        logger.warning("Email delivery not configured — missing SMTP credentials")
        return False

    msg = MIMEText(briefing_text)
    msg["Subject"] = f"Podcast Briefing — {target_date.isoformat()}"
    msg["From"] = smtp_user
    msg["To"] = email_to

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        logger.info("Briefing emailed to %s", email_to)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False


def deliver(
    config: PipelineConfig,
    briefing_text: str,
    target_date: date,
    transcript_sources: dict[str, str],
) -> Path:
    """Save the briefing and optionally deliver via configured method."""
    path = save_briefing(config, briefing_text, target_date, transcript_sources)

    if config.delivery.method == "email":
        deliver_email(briefing_text, target_date)

    return path
