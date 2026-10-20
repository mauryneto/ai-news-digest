# AI News Digest

Daily email digest of AI news from RSS feeds: The Verge, TechCrunch, The Decoder, Ars Technica, MIT Technology Review, Wired, The Information, Simon Willison and Hacker News.

- Runs every day at 06:00 (Brasília) via GitHub Actions and covers the previous 24 hours (06:00 → 06:00).
- **Highlights**: stories covered by the most outlets (Hacker News points break ties).
- **Duplicates merged**: the same story from several outlets becomes one entry with links to each source.
- **Everything else** grouped by topic, one line each.
- No AI/LLM, no paid services.

## Setup

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GMAIL_USER` | Gmail address that sends the digest |
| `GMAIL_APP_PASSWORD` | Gmail app password (https://myaccount.google.com/apppasswords) |
| `MAIL_TO` | Recipients separated by commas, semicolons or spaces (sent as Bcc) |

Manual run: Actions → *Daily AI news digest* → **Run workflow**.

## Local preview

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; use .venv/bin/pip elsewhere
.venv/Scripts/python digest.py --out out/digest.html
```

## Tuning

- Sources: `SOURCES` in `digest.py`. Hacker News comes from the official search API (hn.algolia.com), stories with 100+ points. VentureBeat is not included because its feed blocks RSS readers (HTTP 429).
- Duplicate merging: `--threshold` (default 0.20). Raise it if unrelated stories get merged; lower it if duplicates slip through.
- Topics: keyword lists in `TOPICS`.
