"""Daily AI news digest: fetch RSS feeds, merge duplicate stories, pick highlights, email it.

Usage:
    python digest.py --out out/digest.html     # build only, write HTML preview
    python digest.py --send                    # build and email (needs env vars below)

Env vars for --send: GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO (comma-separated).
"""

from __future__ import annotations

import argparse
import html
import math
import os
import re
import smtplib
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import feedparser

USER_AGENT = "ai-news-digest/1.0 (personal RSS reader; +https://github.com/mauryneto/ai-news-digest)"
BRT = timezone(timedelta(hours=-3))  # Brasília; Brazil has no DST since 2019
DIGEST_HOUR = 6  # window is [yesterday 06:00, today 06:00) in BRT


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    url: str
    ai_filter: bool = False  # feed covers more than AI; keep only AI stories
    paywall: bool = False


# Order = preference when picking which outlet's headline represents a merged story.
SOURCES = [
    Source("verge", "The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    Source("techcrunch", "TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    Source("decoder", "The Decoder", "https://the-decoder.com/feed/"),
    Source("ars", "Ars Technica", "https://arstechnica.com/ai/feed/"),
    Source("mittr", "MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    Source("wired", "Wired", "https://www.wired.com/feed/tag/ai/latest/rss"),
    Source("information", "The Information", "https://www.theinformation.com/feed", ai_filter=True, paywall=True),
    Source("simon", "Simon Willison", "https://simonwillison.net/atom/everything/"),
    Source("hn", "Hacker News", "https://hnrss.org/newest?points=100&count=100", ai_filter=True),
]
SOURCE_RANK = {s.key: i for i, s in enumerate(SOURCES)}

AI_RE = re.compile(
    r"\b(LLMs?|GPT[-\w.]*|ChatGPT|OpenAI|Anthropic|Claude|Gemini|DeepMind|Llama|Mistral|"
    r"DeepSeek|Qwen|Grok|xAI|Copilot|Perplexity|Hugging ?Face|Nvidia|chatbots?|machine learning|"
    r"neural|artificial intelligence|transformers?|diffusion|Midjourney|inference|agentic)\b",
    re.IGNORECASE,
)
# "AI"/"AGI" must be uppercase whole words, otherwise they match ordinary words and names.
AI_CASE_SENSITIVE_RE = re.compile(r"\b(AI|AGI|A\.I\.)(?=\W|$)")

# Keywords match whole words; a trailing "*" matches any word starting with the stem.
TOPICS: list[tuple[str, list[str]]] = [
    ("Policy & Legal", ["lawsuit*", "sue", "sues", "sued", "court*", "judge*", "regulat*", "congress*", "senate",
                         "eu", "ftc", "ban", "bans", "banned", "law", "laws", "copyright*", "sanction*",
                         "government*", "pentagon", "army", "military", "election*", "trump", "white house",
                         "policy", "policies", "antitrust", "custody", "legislat*"]),
    ("Security & Safety", ["security", "hack*", "vulnerab*", "exploit*", "privacy", "leak*", "breach*", "safety",
                            "jailbreak*", "scam*", "deepfake*", "malware", "cyber*", "spying", "spy", "secrets"]),
    ("Money & Business", ["raise", "raises", "raised", "funding", "valuation*", "ipo", "acquir*", "acquisition*",
                           "revenue*", "investor*", "invest*", "series a", "series b", "series c", "series d",
                           "billion", "million", "deal", "deals", "layoff*", "hires", "ceo", "startup*", "profit*", "stock*", "shares"]),
    ("Chips & Infrastructure", ["nvidia", "chip", "chips", "chipmaker*", "gpu*", "data center*", "datacenter*",
                                 "compute", "tpu*", "energy", "power", "semiconductor*", "tsmc", "fossil",
                                 "electricity"]),
    ("Models & Research", ["gpt*", "gemini", "claude", "llama", "mistral", "deepseek", "qwen", "grok",
                            "benchmark*", "paper", "papers", "research*", "study", "studies", "open-weight*",
                            "open source", "reasoning", "training", "model", "models", "scientist*"]),
    ("Products & Tools", ["launch*", "app", "apps", "feature*", "rolls out", "rolling out", "update*", "agent",
                           "agents", "copilot", "chatgpt", "tool", "tools", "api", "browser*", "device*",
                           "glasses", "phone*", "coding", "release*"]),
]
TOPIC_RES = [
    (topic, re.compile(r"\b(?:" + "|".join(
        re.escape(k[:-1]) + r"\w*" if k.endswith("*") else re.escape(k) + r"\b"
        for k in keywords) + ")"))
    for topic, keywords in TOPICS
]
OTHER_TOPIC = "Other"

STOPWORDS = set("""
a an the and or but of to in on at for with from by as is are was were be been being it its this that these
those how why what when who whom which new says say said will can could would should may might after before
over into about than more most just now its it's not no yes your you our we they their them his her he she
i me my up out off all any some via vs per amid while also still first big make makes made get gets using use
uses used here there one two ai report reportedly according
""".split())


@dataclass
class Item:
    source: Source
    title: str
    link: str
    summary: str
    published: datetime
    comments_link: str = ""
    hn_points: int = 0
    tokens: Counter = field(default_factory=Counter)


@dataclass
class Story:
    items: list[Item]
    topic: str = OTHER_TOPIC

    @property
    def lead(self) -> Item:
        # Prefer an outlet with a real summary (HN has none), then by source preference.
        return min(self.items, key=lambda it: (it.source.key == "hn", SOURCE_RANK[it.source.key], -len(it.summary)))

    @property
    def source_keys(self) -> set[str]:
        return {it.source.key for it in self.items}

    @property
    def hn_points(self) -> int:
        return max((it.hn_points for it in self.items), default=0)

    @property
    def newest(self) -> datetime:
        return max(it.published for it in self.items)


# ---------- fetching & parsing ----------

def fetch(source: Source) -> tuple[Source, list[Item] | None, str]:
    last_error = ""
    for _ in range(2):
        try:
            req = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            parsed = feedparser.parse(body)
            if not parsed.entries:
                last_error = "feed returned no entries"
                continue
            return source, [it for e in parsed.entries if (it := to_item(source, e))], ""
        except Exception as exc:  # network errors, HTTP 4xx/5xx, timeouts
            last_error = f"{type(exc).__name__}: {exc}"
    return source, None, last_error


def to_item(source: Source, entry) -> Item | None:
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    title = clean_text(entry.get("title", ""))
    link = entry.get("link", "")
    if not stamp or not title or not link:
        return None
    raw_summary = entry.get("summary", "")
    item = Item(
        source=source,
        title=title,
        link=link,
        summary="" if source.key == "hn" else clean_summary(raw_summary, title),
        published=datetime(*stamp[:6], tzinfo=timezone.utc),
    )
    if source.key == "hn":
        item.comments_link = entry.get("comments", "")
        m = re.search(r"Points:\s*(\d+)", raw_summary)
        item.hn_points = int(m.group(1)) if m else 0
    return item


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_summary(raw: str, title: str) -> str:
    text = clean_text(raw)
    text = re.sub(r"\s*The post .* appeared first on .*$", "", text)
    text = re.sub(r"\s*(Continue reading|Read more|Read the full).*$", "", text, flags=re.IGNORECASE)
    if text.lower().startswith(title.lower()):  # Simon Willison repeats the title
        text = text[len(title):].lstrip(" :-—")
    return text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence_end > limit * 0.6:
        return cut[: sentence_end + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:—-") + "…"


def is_ai(item: Item) -> bool:
    text = f"{item.title} {item.summary}"
    return bool(AI_CASE_SENSITIVE_RE.search(text) or AI_RE.search(text))


# ---------- dedup / clustering ----------

def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.\-+]*[a-z0-9]|[a-z0-9]", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def build_vectors(items: list[Item]) -> list[dict[str, float]]:
    for it in items:
        lead_words = " ".join(it.summary.split()[:40])
        it.tokens = Counter(tokenize(it.title) * 2 + tokenize(lead_words))
    doc_freq = Counter(tok for it in items for tok in it.tokens)
    n = len(items)
    vectors = []
    for it in items:
        vec = {t: c * math.log((1 + n) / (1 + doc_freq[t])) for t, c in it.tokens.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({t: v / norm for t, v in vec.items()})
    return vectors


def normalize_link(url: str) -> str:
    url = re.sub(r"^https?://(www\.)?", "", url.split("#")[0].split("?")[0])
    return url.rstrip("/").lower()


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def cluster(items: list[Item], threshold: float) -> list[Story]:
    """Agglomerative clustering over TF-IDF title+lead vectors.

    Two groups merge when their closest pair reaches `threshold` and their average similarity
    reaches 60% of it, so one strong link can join a story without letting weak links chain.
    """
    vectors = build_vectors(items)
    links = [normalize_link(it.link) for it in items]
    n = len(items)
    # Same article URL from several feeds (e.g. HN linking an outlet's story) is the same story.
    sim = [[1.0 if i == j or links[i] == links[j] else cosine(vectors[i], vectors[j]) for j in range(n)]
           for i in range(n)]
    groups: list[list[int]] = [[i] for i in range(n)]
    while True:
        best, pair = threshold, None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                ga, gb = groups[a], groups[b]
                cross = [sim[i][j] for i in ga for j in gb]
                strongest, avg = max(cross), sum(cross) / len(cross)
                if strongest >= best and avg >= threshold * 0.6:
                    best, pair = strongest, (a, b)
        if pair is None:
            break
        a, b = pair
        groups[a] += groups.pop(b)
    return [Story(items=sorted((items[i] for i in g), key=lambda it: SOURCE_RANK[it.source.key])) for g in groups]


def classify(story: Story) -> str:
    text = " ".join(f"{it.title} {it.summary}" for it in story.items).lower()
    scores = []
    for priority, (topic, pattern) in enumerate(TOPIC_RES):
        hits = len(pattern.findall(text))
        if hits:
            scores.append((-hits, priority, topic))
    return min(scores)[2] if scores else OTHER_TOPIC


def pick_highlights(stories: list[Story], max_n: int = 5, min_n: int = 3) -> list[Story]:
    """Stories covered by the most outlets are what mattered; HN points break ties and fill quiet days."""
    def rank(s: Story):
        return (len(s.source_keys), s.hn_points, s.newest)

    multi = sorted((s for s in stories if len(s.source_keys) >= 2), key=rank, reverse=True)[:max_n]
    if len(multi) < min_n:
        popular = sorted((s for s in stories if s not in multi and s.hn_points >= 200),
                         key=lambda s: s.hn_points, reverse=True)
        multi += popular[: min_n - len(multi)]
    return multi


# ---------- rendering ----------

def window_for(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(BRT)
    end = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


def source_links(story: Story) -> str:
    parts = []
    for it in story.items:
        label = it.source.name + (" 🔒" if it.source.paywall else "")
        if it.source.key == "hn":
            target = it.comments_link or it.link
            label = f"HN · {it.hn_points} pts"
        else:
            target = it.link
        parts.append(f'<a href="{html.escape(target)}" style="color:#5b6b8c;text-decoration:none">{html.escape(label)}</a>')
    return " · ".join(parts)


def render_html(highlights: list[Story], by_topic: dict[str, list[Story]], start: datetime, end: datetime,
                total: int, failed: list[str]) -> str:
    esc = html.escape
    out = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;'
        'margin:0 auto;color:#1f2430;line-height:1.45;font-size:15px">',
        f'<h1 style="font-size:22px;margin:0 0 4px">AI News · {end.strftime("%a %d %b")}</h1>',
        f'<div style="color:#6b7280;font-size:13px;margin-bottom:20px">{total} stories from '
        f'{start.strftime("%a %H:%M")} to {end.strftime("%a %H:%M")} (Brasília)</div>',
    ]
    if highlights:
        out.append('<h2 style="font-size:17px;margin:24px 0 8px;border-bottom:2px solid #f59e0b;padding-bottom:4px">'
                   '⭐ Highlights</h2>')
        for s in highlights:
            lead = s.lead
            covered = f"{len(s.source_keys)} sources" if len(s.source_keys) > 1 else ""
            out.append(
                '<div style="margin:0 0 16px">'
                f'<a href="{esc(lead.link)}" style="font-weight:600;font-size:16px;color:#111827;text-decoration:none">'
                f'{esc(lead.title)}</a>'
                + (f'<div style="margin-top:3px">{esc(truncate(lead.summary, 320))}</div>' if lead.summary else "")
                + f'<div style="font-size:13px;margin-top:3px;color:#6b7280">{covered}'
                f'{" — " if covered else ""}{source_links(s)}</div></div>'
            )
    for topic, stories in by_topic.items():
        out.append(f'<h2 style="font-size:16px;margin:24px 0 8px;border-bottom:1px solid #e5e7eb;padding-bottom:4px">'
                   f'{esc(topic)} <span style="color:#9ca3af;font-weight:400">({len(stories)})</span></h2>')
        for s in stories:
            lead = s.lead
            summary = f' — <span style="color:#374151">{esc(truncate(lead.summary, 150))}</span>' if lead.summary else ""
            out.append(
                '<div style="margin:0 0 9px">'
                f'<a href="{esc(lead.link)}" style="font-weight:600;color:#111827;text-decoration:none">{esc(lead.title)}</a>'
                f'{summary} <span style="font-size:12px">· {source_links(s)}</span></div>'
            )
    if not highlights and not by_topic:
        out.append("<p>No AI stories in this window.</p>")
    footer = "Sources: " + ", ".join(s.name for s in SOURCES)
    if failed:
        footer += f'<br><b style="color:#b45309">Could not fetch:</b> {esc(", ".join(failed))}'
    out.append(f'<div style="margin-top:28px;color:#9ca3af;font-size:12px">{footer}</div></div>')
    return "\n".join(out)


def render_text(highlights: list[Story], by_topic: dict[str, list[Story]]) -> str:
    lines = []
    if highlights:
        lines.append("HIGHLIGHTS")
        for s in highlights:
            lines += [f"* {s.lead.title}", f"  {truncate(s.lead.summary, 320)}", f"  {s.lead.link}", ""]
    for topic, stories in by_topic.items():
        lines.append(topic.upper())
        lines += [f"- {s.lead.title} — {s.lead.link}" for s in stories]
        lines.append("")
    return "\n".join(lines) or "No AI stories in this window."


# ---------- main ----------

def build(now: datetime, threshold: float):
    start, end = window_for(now)
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        results = list(pool.map(fetch, SOURCES))

    failed, items = [], []
    for source, fetched, error in results:
        if fetched is None:
            failed.append(source.name)
            print(f"[warn] {source.name}: {error}", file=sys.stderr)
            continue
        kept = [it for it in fetched if start <= it.published < end and (not source.ai_filter or is_ai(it))]
        print(f"[info] {source.name}: {len(kept)} in window ({len(fetched)} in feed)", file=sys.stderr)
        items += kept
    if len(failed) == len(SOURCES):
        raise SystemExit("All feeds failed; not sending.")

    stories = cluster(items, threshold) if items else []
    for s in stories:
        s.topic = classify(s)
    highlights = pick_highlights(stories)
    rest = [s for s in stories if s not in highlights]
    by_topic: dict[str, list[Story]] = {}
    for topic in [t for t, _ in TOPICS] + [OTHER_TOPIC]:
        group = sorted((s for s in rest if s.topic == topic), key=lambda s: (len(s.source_keys), s.newest), reverse=True)
        if group:
            by_topic[topic] = group
    return start, end, items, highlights, by_topic, failed


def send(subject: str, html_body: str, text_body: str) -> None:
    user = os.environ["GMAIL_USER"].strip()
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").strip()
    recipients = [r.strip() for r in os.environ["MAIL_TO"].split(",") if r.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"AI News Digest <{user}>"
    msg["To"] = user  # recipients go in Bcc so they don't see each other
    msg.set_content(text_body)
    msg.add_alternative(f"<html><body>{html_body}</body></html>", subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg, from_addr=user, to_addrs=recipients)
    print(f"[info] sent to {len(recipients)} recipient(s)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="write the HTML email to this file")
    parser.add_argument("--send", action="store_true", help="send the email via Gmail SMTP")
    parser.add_argument("--threshold", type=float, default=0.20, help="similarity needed to merge stories")
    args = parser.parse_args()

    start, end, items, highlights, by_topic, failed = build(datetime.now(timezone.utc), args.threshold)
    n_stories = len(highlights) + sum(len(v) for v in by_topic.values())
    html_body = render_html(highlights, by_topic, start, end, n_stories, failed)
    subject = f"AI News · {end.strftime('%a %d %b')} · {len(highlights)} highlights · {n_stories} stories"
    print(f"[info] {len(items)} items -> {n_stories} stories, {len(highlights)} highlights", file=sys.stderr)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(f'<!doctype html><meta charset="utf-8"><title>{html.escape(subject)}</title>'
                            f'<body style="background:#fff">{html_body}</body>', encoding="utf-8")
        print(f"[info] wrote {args.out}", file=sys.stderr)
    if args.send:
        send(subject, html_body, render_text(highlights, by_topic))


if __name__ == "__main__":
    main()
