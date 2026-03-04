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
Cron (7am PT) → Fetch RSS → Download Audio → Transcribe (Moonshine) → Analyze (Gemini) → Deliver
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
# Edit config/.env with your GEMINI_API_KEY (free at https://aistudio.google.com/apikey)
```

## Usage

```bash
# Run today's pipeline
podcast-updates

# Run for a specific date
podcast-updates 2026-03-03
```

## Configuration

Edit `config/shows.yaml` to add/remove shows, change transcription engine, or adjust delivery method. The analysis prompt is in `config/prompt.md`.

## Architecture

- **Platform:** Raspberry Pi 5 (16GB, 512GB NVMe)
- **Transcription:** Moonshine Medium (primary) or faster-whisper (fallback)
- **Analysis:** Google Gemini 2.0 Flash (free tier)
- **Cost:** $0/month
