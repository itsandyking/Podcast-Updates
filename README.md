# Podcast Updates

Automated daily pipeline that ingests transcripts from multiple daily news podcasts, identifies story overlap and divergence across shows, and produces a single synthesized briefing.

## What it answers each day

1. **What does everyone agree matters?** — Consensus stories covered by 2+ shows
2. **What did only one show cover?** — Unique coverage signaling editorial priority
3. **How do the angles differ?** — Same story, different framing

## Target Shows

| Show | Publisher | Format | Length |
|------|-----------|--------|--------|
| Up First | NPR | Survey (3-4 segments) | 10-15 min |
| The Daily | NYT | Single deep-dive | 25-35 min |
| Today, Explained | Vox | Single topic, analytical | 25-30 min |
| Consider This | NPR | 1-2 stories, mid-depth | 15-20 min |

## Pipeline

```
Cron (7am PT) → Fetch RSS → Download Audio → Transcribe (Moonshine) → Analyze (Claude) → Deliver
```

NPR shows check for web-published transcripts first to save compute.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

# For local transcription (pick one):
pip install -e ".[moonshine]"   # Recommended for Pi
pip install -e ".[whisper]"     # Alternative

# Configure
cp config/.env.example config/.env
# Edit config/.env with your ANTHROPIC_API_KEY
```

## Usage

```bash
# Run today's pipeline
podcast-updates

# Run for a specific date
podcast-updates 2026-03-03
```

## Configuration

Edit `config/shows.yaml` to add/remove shows, change transcription engine, or adjust delivery method. The Claude analysis prompt is in `config/prompt.md`.

## Architecture

- **Platform:** Raspberry Pi 5 (16GB, 512GB NVMe)
- **Transcription:** Moonshine Medium (primary) or faster-whisper (fallback)
- **Analysis:** Claude Sonnet 4.5 via Anthropic API
- **Cost:** ~$2-3/month (API only)
