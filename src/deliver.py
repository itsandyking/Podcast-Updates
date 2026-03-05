"""Format and deliver the daily briefing."""

from __future__ import annotations

import logging
import smtplib
import os
from datetime import date, datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .config import DATA_DIR, ROOT_DIR, PipelineConfig

logger = logging.getLogger(__name__)

BRIEFINGS_DIR = DATA_DIR / "briefings"
DAILY_TRANSCRIPTS_DIR = ROOT_DIR / "daily_transcripts"


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


def save_daily_transcripts(
    config: PipelineConfig,
    transcripts: dict[str, str],
    target_date: date,
) -> Path:
    """Save transcripts as individual MD files and a combined file with prompt."""
    date_str = target_date.isoformat()
    if config.group:
        dest_dir = DAILY_TRANSCRIPTS_DIR / date_str / config.group
    else:
        dest_dir = DAILY_TRANSCRIPTS_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = (ROOT_DIR / config.delivery.claude_prompt_file).read_text()

    # Save individual transcript files
    for show in config.shows:
        if show.slug in transcripts:
            (dest_dir / f"{show.slug}.md").write_text(transcripts[show.slug])

    # Build combined file: prompt at top, then each transcript
    blocks = [prompt_text]
    for show in config.shows:
        if show.slug not in transcripts:
            continue
        blocks.append(
            f"## {show.name} ({show.publisher})\n"
            f"*{show.format}, ~{show.typical_length_min} min — {target_date.isoformat()}*\n\n"
            f"{transcripts[show.slug]}"
        )

    combined = "\n\n---\n\n".join(blocks)
    combined_path = dest_dir / "all-transcripts.md"
    combined_path.write_text(combined)

    logger.info("Saved %d transcripts to %s", len(transcripts), dest_dir)
    return combined_path


def deliver_transcripts_email(combined_path: Path, target_date: date) -> bool:
    """Email the combined transcripts MD file as an attachment."""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    email_to = os.environ.get("EMAIL_TO", "")

    if not all([smtp_user, smtp_pass, email_to]):
        logger.warning("Email delivery not configured — missing SMTP credentials")
        return False

    msg = MIMEMultipart()
    msg["Subject"] = f"Podcast Transcripts — {target_date.isoformat()}"
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg.attach(MIMEText(
        "Today's podcast transcripts are attached. "
        "Upload the MD file to Claude for cross-show analysis."
    ))

    attachment = MIMEBase("text", "markdown")
    attachment.set_payload(combined_path.read_bytes())
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition", "attachment", filename=combined_path.name
    )
    msg.attach(attachment)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        logger.info("Transcripts emailed to %s", email_to)
        return True
    except Exception as e:
        logger.error("Failed to send transcripts email: %s", e)
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
