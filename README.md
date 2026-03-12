# Podcast Updates

Automated pipeline running on a Raspberry Pi 5 that fetches podcast transcripts, runs cross-show analysis via Gemini, and emails a synthesized briefing. Four separate pipelines run on independent schedules: daily news, and three weekly topic groups (tech, finance, parenting).

---

## Pipelines

### News (daily)
Runs Mon–Sat. Answers: what does everyone agree matters today, what did only one show cover, and how do the angles differ?

| Show | Publisher | Format | Length | Transcript |
|---|---|---|---|---|
| Up First | NPR | Survey | ~12 min | Web (NPR) |
| The Daily | NYT | Deep-dive | ~28 min | Audio |
| Today, Explained | Vox | Analytical | ~27 min | Audio |
| Consider This | NPR | Mid-depth | ~17 min | Web (NPR) |
| The Headlines | NYT | Survey | ~9 min | Audio |
| Headlines From The Times | L.A. Times | Survey | ~9 min | Audio |
| Apple News Today | Apple News | Survey | ~14 min | Audio |
| WSJ's Take on the Week | WSJ | Deep-dive | ~31 min | Audio |

Config: `config/shows.yaml` — Prompt: `config/prompt_claude.md`

---

### Tech (weekly)
Runs Fridays after Hard Fork drops at noon UTC. Covers product moves, AI developments, investigative angles, and platform accountability.

| Show | Publisher | Format | Length | Publishes |
|---|---|---|---|---|
| Hard Fork | NYT | Deep-dive | ~60 min | Friday ~noon UTC |
| 404 Media | 404 Media | Investigative | ~45 min | Mon + Wed 11am UTC |
| Decoder | The Verge | CEO interview | ~60 min | Mon + Thu 10am UTC |

Config: `config/shows_tech.yaml` — Prompt: `config/prompt_claude_tech.md`

---

### Finance (weekly)
Runs Fridays 3 hours after the tech pipeline (staggered to avoid Pi resource overlap). Covers markets, macro, personal finance, and policy.

| Show | Publisher | Format | Length | Publishes |
|---|---|---|---|---|
| Animal Spirits | The Compound | Markets commentary | ~60 min | Mon + Wed 9am UTC |
| The Bid | BlackRock | Institutional macro | ~30 min | Friday 5am UTC |
| Money for Couples | Ramit Sethi | Personal finance | ~45 min | Tuesday 11am UTC |
| WashingtonWise | Charles Schwab | Policy/investing | ~30 min | Thursday 8am UTC (bi-weekly) |

Config: `config/shows_finance.yaml` — Prompt: `config/prompt_claude_finance.md`

> WashingtonWise is bi-weekly — it will be skipped on off weeks.

---

### Parenting (weekly)
Runs Tuesdays after both shows publish. Covers frameworks, research, and practical takeaways from attachment/respectful parenting shows.

| Show | Publisher | Format | Length | Publishes |
|---|---|---|---|---|
| Good Inside with Dr. Becky | Good Inside | Framework-heavy | ~45 min | Tuesday 6:30am UTC |
| Respectful Parenting: Janet Lansbury Unruffled | JLML Press | Listener Q&A | ~30 min | Tuesday 8am UTC |

Config: `config/shows_parenting.yaml` — Prompt: `config/prompt_claude_parenting.md`

---

## Schedule

Cron runs in the Pi's local timezone (`America/Los_Angeles`), so times are PT and DST is handled automatically.

| Pipeline | Cron | PT | Day(s) |
|---|---|---|---|
| News | `0 7 * * 1-6` | 7am | Mon–Sat |
| Tech | `0 17 * * 5` | 5pm | Friday |
| Finance | `0 9 * * 6` | 9am | Saturday |
| Parenting | `0 6 * * 2` | 6am | Tuesday |

### Crontab entries

```
# news — Mon–Sat 7am PT
0 7 * * 1-6  /home/piking5/Podcast-Updates/.venv/bin/podcast-updates

# tech — Fri 5pm PT (Hard Fork ~5am, Pivot ~3am, Big Technology up to ~3pm — all available)
0 17 * * 5   cd /home/piking5/Podcast-Updates && .venv/bin/python -m src.pipeline --config config/shows_tech.yaml

# finance — Sat 9am PT (catches We Study Billionaires Sat drops; no Pi overlap with tech)
0 9 * * 6    cd /home/piking5/Podcast-Updates && .venv/bin/python -m src.pipeline --config config/shows_finance.yaml

# parenting — Tue 6am PT
0 6 * * 2    cd /home/piking5/Podcast-Updates && .venv/bin/python -m src.pipeline --config config/shows_parenting.yaml
```

---

## How It Works

```
Cron trigger
    → Fetch RSS feeds (7-day window for weekly groups, 24h for news)
    → For each show: check for web transcript, else download audio
    → Transcribe audio locally (faster-whisper)
    → Save individual + combined transcript files
    → Email combined file as attachment
```

NPR shows use web-published transcripts to save compute. All other shows download and transcribe audio locally. Weekly group outputs go to `daily_transcripts/{date}/{group}/`, news goes to `daily_transcripts/{date}/`.

The combined transcript file is emailed as a Markdown attachment with the Claude analysis prompt prepended — open it in Claude.ai to generate the briefing.

---

## Email Formatting

Briefing emails are sent as `multipart/alternative` with a plain-text fallback and a styled HTML part. The HTML is rendered from the Markdown output and styled to match the Claude iOS chat aesthetic:

- **Font:** `-apple-system` / SF Pro Text stack
- **Background:** `#F2F2F7` (iOS system gray), white rounded card (18px radius)
- **Accents:** `#007AFF` iOS blue for the header and links
- **Typography:** weighted `h1`/`h2`/`h3` hierarchy, semibold `strong`, italic `em`, monospace code blocks with `#F5F5F7` background
- **Dividers/quotes:** subtle `#E5E5EA` rules and left-bordered blockquotes

All four pipelines (news, tech, finance, parenting) use the same `deliver_email` function, so the styling applies everywhere.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e ".[whisper]"   # local transcription

cp config/.env.example config/.env
# Add: GEMINI_API_KEY, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO
```

Get a free Gemini API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

---

## Running Manually

```bash
# news — today
python -m src.pipeline

# news — specific date
python -m src.pipeline 2026-03-03

# weekly groups
python -m src.pipeline --config config/shows_tech.yaml
python -m src.pipeline --config config/shows_finance.yaml
python -m src.pipeline --config config/shows_parenting.yaml

# weekly group — specific date (useful for testing with a past date)
python -m src.pipeline --config config/shows_tech.yaml 2026-02-28
```

---

## Config Files

| File | Purpose |
|---|---|
| `config/shows.yaml` | News pipeline shows + settings |
| `config/shows_tech.yaml` | Tech pipeline shows + settings |
| `config/shows_finance.yaml` | Finance pipeline shows + settings |
| `config/shows_parenting.yaml` | Parenting pipeline shows + settings |
| `config/prompt.md` | Gemini analysis prompt (news) |
| `config/prompt_tech.md` | Gemini analysis prompt (tech) |
| `config/prompt_finance.md` | Gemini analysis prompt (finance) |
| `config/prompt_parenting.md` | Gemini analysis prompt (parenting) |
| `config/prompt_claude.md` | Claude email prompt (news) |
| `config/prompt_claude_tech.md` | Claude email prompt (tech) |
| `config/prompt_claude_finance.md` | Claude email prompt (finance) |
| `config/prompt_claude_parenting.md` | Claude email prompt (parenting) |
| `config/prompt_extract.md` | Transcript extraction prompt (shared) |
| `config/.env` | Secrets — API keys, SMTP credentials |

---

## Platform

- **Hardware:** Raspberry Pi 5 (16GB RAM, 512GB NVMe)
- **Transcription:** faster-whisper (tiny model)
- **Analysis:** Claude Sonnet 4.6 (Anthropic API, ~$8/month)
- **Cost:** ~$8/month
