# 🎓 Scholarship Scout Pro

A long-running, polite web crawler that discovers **fully-funded Master's
scholarships** for **Bangladeshi CSE graduates** in English-taught programmes
across Europe (Fall 2026 / Spring 2027) and Australia & New Zealand
(Semester 1 / 2 of 2027).

Built with **Streamlit** (UI) + **Playwright async** (crawler) + **SQLite**
(state) + **openpyxl** (Excel export).

---

## What it does

- Walks a country-by-country, phase-by-phase queue (university → government →
  external → joint) defined in `config.yaml`.
- Avoids bot detection: stealth fingerprints, rotating User-Agents, randomised
  viewports, human-like delays (3–7 s), per-domain rate limit (≥ 1 s),
  exponential backoff on 429 / 503 / 504, robots.txt respect, optional proxy
  rotation.
- Filters every listing through five hard checks (Bangladesh-eligible, field
  match, intake match, funding tier, English-taught) and produces a
  `verification_status` (`confirmed` / `probable` / `rejected`) plus a 0–100
  confidence score.
- Ranks results: tier → AI/ML relevance → CSE relevance → intake match →
  deadline urgency → work rights.
- Pause / Resume safely. Each pause auto-exports a per-session Excel.
- Exports a 3-sheet workbook: **Confirmed**, **Needs Manual Review** (probable,
  score ≥ 70), **Session Log** — with frozen header, AutoFilter, hyperlinked
  Application URL, dedup on URL.

---

## Project layout

```
scholarship-scrawler/
├── app.py                  # Streamlit dashboard
├── scraper_engine.py       # Async Playwright crawler (stealth + backoff + robots.txt)
├── verifier.py             # 5-check eligibility + confidence score
├── ranker.py               # Tier-aware ranking
├── exporter.py             # 3-sheet Excel writer (openpyxl)
├── db.py                   # SQLite schema + helpers
├── session_manager.py      # JSON session state (pause / resume / dedup)
├── config.yaml             # Applicant profile, targets, scraper settings, seeds
├── selectors.yaml          # Per-host CSS / XPath selectors (editable, no Python edits needed)
├── proxies.txt             # Optional proxy rotation
├── requirements.txt
├── state/                  # Runtime: SQLite DB + session_state.json
└── outputs/                # Runtime: Excel exports
```

---

## Setup

### 1. Prerequisites

- **Python 3.10+**
- About 500 MB of disk for the Chromium browser Playwright downloads.

### 2. Clone and install

```bash
git clone https://github.com/samiulshawon/scholarship-scrawler.git
cd scholarship-scrawler

python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium     # one-time browser download
```

### 3. (Optional) Add proxies

Drop one proxy URL per line in `proxies.txt`:

```
http://user:pass@host:8080
socks5://10.0.0.5:1080
```

The scraper picks one at random per session. Leave the file empty to crawl
without proxies.

### 4. (Optional) Tune `config.yaml`

- Add or remove **seed URLs** under `seeds:`. The shipped file has
  university / government / external / joint URLs for several countries; extend
  it as you discover sources.
- Adjust **delays**, **retry policy**, or **headless** mode under `scraper:`.

### 5. (Optional) Add per-site selectors in `selectors.yaml`

If a source's HTML structure is unique, add a section keyed on the hostname.
The crawler falls back to the `default:` selectors if no host entry exists.
**You can ship per-site fixes without touching Python.**

---

## Run

```bash
streamlit run app.py
```

Streamlit opens at <http://localhost:8501>.

In the sidebar:

| Button | What it does |
|---|---|
| ▶ **Start** | Begins a fresh session at the top of the queue |
| ⏸ **Pause** | Stops cleanly after the current page; auto-exports a per-session Excel |
| ▶ **Resume** | Continues from where the last session paused; skips visited URLs |
| 📥 **Export Excel** | Exports the current session **or** all sessions (toggle above the button) |

The main panel shows live progress (current country / phase / pages /
records), country-grouped tier-bucketed results, and a log stream with
sub-tabs for Crawl Activity, Errors, Blocked Sources, and Verification.

---

## CLI usage (no UI)

```bash
python scraper_engine.py            # start a fresh session
```

For programmatic resume:

```python
import asyncio
from scraper_engine import run_from_files

asyncio.run(run_from_files(resume=True))
```

---

## Output files

- `state/session.db` — SQLite database (scholarships, visited URLs, sessions,
  domain blocks).
- `state/session_state.json` — paused/resumable session snapshot.
- `outputs/scholarships_session_<id>.xlsx` — per-session export, written on
  every pause.
- `outputs/scholarships_all_<timestamp>.xlsx` — full cross-session export.

All exports are deduplicated on `Application URL`.

---

## How filtering works

| Check | Pass criteria |
|---|---|
| `bangladesh_accepted` | "Bangladesh" / "all nationalities" / "open to all" / "developing countries" / "ODA countries" |
| `field_matches` | AI / ML / CV / NLP / Data Science / Core CSE keywords |
| `intake_matches` | Fall 2026, Spring 2027, Semester 1 / 2 of 2027 |
| `funding_tier_valid` | Classified as Tier 1 (Platinum) → Tier 4 (Bronze); discards partial / loan |
| `language_english` | Programme explicitly in English |

Score ≥ 85 **and** all hard gates pass → `confirmed`.
Score ≥ 70 with Bangladesh + field gates pass → `probable` (Sheet 2 review).
Anything else → `rejected` (logged only, never stored).

---

## Extending the crawler

**Add a new country source:** edit `config.yaml`, append URLs under the country
in `seeds:` — phase order is preserved automatically.

**Fix a parser for a tricky site:** add a host block in `selectors.yaml`. No
Python edits.

**Write a bespoke adapter (when CSS isn't enough):**

```python
# my_site.py
async def parse_my_site(html: str, page_url: str, config: dict) -> list[dict]:
    ...

# wire it up before engine.run()
engine.register_site_adapter("scholarships.example.edu", parse_my_site)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Playwright is not installed` | `pip install playwright && playwright install chromium` |
| Crawler stops on a domain after a few pages | That host hit the block threshold (`scraper.domain_block_threshold` in `config.yaml`). Try a proxy or relax the threshold. |
| Streamlit "session ended" mid-crawl | Refresh; the crawler thread keeps running and `session_state.json` preserves position. |
| No results after a long run | Open the **Errors** and **Blocked / Skipped** log tabs — most likely cause is bot-protection on a major source; add per-site selectors or a proxy. |
| Excel column too narrow | All columns auto-fit on export; widen manually in Excel after opening. |

---

## Notes on politeness

This crawler is designed to be a respectful guest:

- Honours `robots.txt`.
- Never exceeds 1 request / second per host.
- Random 3–7 s human-like delay between pages.
- Backs off exponentially on 429 / 503 / 504.
- Abandons a domain after repeated blocks rather than hammering it.

If you change `scraper.min_delay_seconds` below 3 s or the per-domain interval
below 1 s, you may get blocked — and you also stop being polite. Don't do that
unless you know what you're doing.
