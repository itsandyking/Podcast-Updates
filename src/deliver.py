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

import markdown as md

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


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    /* Base — iOS system background */
    body {{
      margin: 0;
      padding: 20px 12px;
      background-color: #F2F2F7;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      font-size: 16px;
      color: #1C1C1E;
      -webkit-font-smoothing: antialiased;
    }}
    /* Card — mimics a Claude response bubble */
    .card {{
      max-width: 680px;
      margin: 0 auto;
      background: #FFFFFF;
      border-radius: 18px;
      padding: 28px 32px 32px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    }}
    /* Header bar */
    .header {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid #E5E5EA;
    }}
    .header-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #007AFF;
      flex-shrink: 0;
    }}
    .header-title {{
      font-size: 13px;
      font-weight: 600;
      color: #007AFF;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    .header-date {{
      margin-left: auto;
      font-size: 13px;
      color: #8E8E93;
    }}
    /* Typography */
    h1 {{
      font-size: 22px;
      font-weight: 700;
      color: #1C1C1E;
      margin: 0 0 8px;
      line-height: 1.25;
    }}
    h2 {{
      font-size: 18px;
      font-weight: 600;
      color: #1C1C1E;
      margin: 28px 0 8px;
      line-height: 1.3;
    }}
    h3 {{
      font-size: 16px;
      font-weight: 600;
      color: #3A3A3C;
      margin: 20px 0 6px;
      line-height: 1.35;
    }}
    p {{
      margin: 0 0 14px;
      line-height: 1.6;
      color: #1C1C1E;
    }}
    ul, ol {{
      margin: 0 0 14px;
      padding-left: 22px;
      line-height: 1.6;
    }}
    li {{
      margin-bottom: 5px;
      color: #1C1C1E;
    }}
    strong {{
      font-weight: 600;
      color: #1C1C1E;
    }}
    em {{
      font-style: italic;
      color: #3C3C43;
    }}
    a {{
      color: #007AFF;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    code {{
      font-family: "SF Mono", "Menlo", "Monaco", "Courier New", monospace;
      font-size: 14px;
      background: #F5F5F7;
      color: #1C1C1E;
      padding: 2px 6px;
      border-radius: 5px;
    }}
    pre {{
      background: #F5F5F7;
      border-radius: 10px;
      padding: 16px 18px;
      overflow-x: auto;
      margin: 0 0 16px;
    }}
    pre code {{
      background: none;
      padding: 0;
      font-size: 14px;
    }}
    blockquote {{
      margin: 0 0 14px;
      padding: 10px 16px;
      border-left: 3px solid #C6C6C8;
      color: #3C3C43;
      font-style: italic;
    }}
    hr {{
      border: none;
      border-top: 1px solid #E5E5EA;
      margin: 24px 0;
    }}
    /* Footer */
    .footer {{
      margin-top: 28px;
      padding-top: 16px;
      border-top: 1px solid #E5E5EA;
      font-size: 12px;
      color: #8E8E93;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="header-dot"></div>
      <span class="header-title">Podcast Briefing</span>
      <span class="header-date">{date}</span>
    </div>
    {body}
    <div class="footer">Generated by Podcast Updates &middot; {date}</div>
  </div>
</body>
</html>"""


def _briefing_to_html(briefing_text: str, target_date: date) -> str:
    """Convert a markdown briefing to a styled HTML email."""
    body_html = md.markdown(
        briefing_text,
        extensions=["extra", "sane_lists"],
    )
    return _HTML_TEMPLATE.format(
        date=target_date.strftime("%B %-d, %Y"),
        body=body_html,
    )


def _build_episode_header(episodes: list) -> str:
    """Build a markdown episode list to prepend to the briefing."""
    lines = ["### Episodes\n"]
    for ep in episodes:
        pub = ep.published.strftime("%-d %B %Y")
        lines.append(f"- **{ep.show_name}** — {ep.title} *({pub})*")
    return "\n".join(lines)


def deliver_email(briefing_text: str, target_date: date, episodes: list | None = None) -> bool:
    """Send the briefing via email with plain-text and HTML alternatives."""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    email_to = os.environ.get("EMAIL_TO", "")

    if not all([smtp_user, smtp_pass, email_to]):
        logger.warning("Email delivery not configured — missing SMTP credentials")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Podcast Briefing — {target_date.strftime('%B %-d, %Y')}"
    msg["From"] = smtp_user
    msg["To"] = email_to

    if episodes:
        header = _build_episode_header(episodes)
        full_text = f"{header}\n\n---\n\n{briefing_text}"
    else:
        full_text = briefing_text

    msg.attach(MIMEText(full_text, "plain", "utf-8"))
    msg.attach(MIMEText(_briefing_to_html(full_text, target_date), "html", "utf-8"))

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
