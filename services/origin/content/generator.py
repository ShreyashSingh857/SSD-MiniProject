"""Deterministic generator for the simulated newspaper's content.

Everything is fictional (the paper is "The Daily Edge", set in the made-up city of Port Halden),
so the dataset can be shipped and regenerated freely. Articles, hero images, thumbnails and
static assets are produced here and seeded into MinIO by the origin service.
"""
from __future__ import annotations

import io
import json
import os
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

GENERATOR_VERSION = "3"
STATIC_DIR = Path(__file__).parent / "static"
# JPEGs rendered at image build time, because rendering is slow on small hosted instances.
IMAGE_CACHE_DIR = Path(os.getenv("IMAGE_CACHE_DIR", "/app/image-cache"))

SECTIONS = {
    "world": (37, 99, 235),
    "politics": (185, 28, 28),
    "business": (5, 150, 105),
    "technology": (124, 58, 237),
    "sports": (234, 88, 12),
    "science": (8, 145, 178),
    "health": (219, 39, 119),
    "culture": (161, 98, 7),
}

CITIES = ["Port Halden", "Norrvik", "San Aurelio", "Kestrel Bay", "Ashford Vale", "Lumen City",
          "Okaro", "Bellmere", "Tarrow", "Vienne-sur-Lac"]
PEOPLE = ["Mayor Elena Ruiz", "Minister Tomas Okafor", "Dr. Priya Raman", "Coach Malik Brennan",
          "CEO Hana Sato", "Professor Ingrid Holm", "Senator David Achebe", "Chief Engineer Lena Park",
          "Commissioner Ravi Menon", "Dr. Amara Collins"]
TEAMS = ["Port Halden Mariners", "Norrvik Wolves", "Kestrel Bay Falcons", "Bellmere United",
         "Tarrow Rovers", "Lumen City Comets"]
COMPANIES = ["Halden Freight", "Norrlight Energy", "Corvane Systems", "Bluefin Retail",
             "Atlas Biotech", "Meridian Bank", "Quillworks", "Vantor Motors"]
AUTHORS = ["Sara Lindqvist", "Omar Haddad", "Mei Chen", "Jonas Weber", "Aisha Bello", "Lucas Moreau",
           "Nadia Petrova", "Kenji Watanabe", "Rosa Delgado", "Femi Adeyemi"]

HEADLINES = {
    "world": [
        "{city} hosts emergency summit as river levels reach record high",
        "Trade corridor between {city} and {city2} reopens after six-week closure",
        "Coastal nations agree on shared early-warning system for storms",
        "Aid convoys reach {city} as relief effort enters second week",
        "{person} calls for ceasefire talks to resume in neutral {city}",
    ],
    "politics": [
        "{person} unveils housing plan promising 40,000 new homes",
        "Council vote on {city} transit expansion postponed after heated debate",
        "Opposition demands inquiry into delayed infrastructure budget",
        "{person} survives confidence vote by narrow margin",
        "New transparency law requires live publication of lobbying meetings",
    ],
    "business": [
        "{company} shares jump 12% after surprise quarterly profit",
        "{company} to open regional hub in {city}, creating 1,200 jobs",
        "Central bank holds rates steady, signals cut later this year",
        "{company} and {company2} announce merger talks",
        "Small businesses in {city} report strongest holiday season in a decade",
    ],
    "technology": [
        "{company} unveils edge computing network spanning 40 cities",
        "Researchers in {city} demonstrate battery that charges in five minutes",
        "City of {city} rolls out free public Wi-Fi across all tram lines",
        "{company} outage disrupts payments for millions of users",
        "Open-source project to cut data-centre energy use gains major backers",
    ],
    "sports": [
        "{team} clinch title with stoppage-time winner",
        "{team} sign record-breaking striker ahead of derby",
        "Marathon in {city} draws 30,000 runners despite heavy rain",
        "{coach} steps down after eight seasons with {team}",
        "{team} and {team2} play out thrilling 3-3 draw",
    ],
    "science": [
        "Telescope array spots water vapour on distant exoplanet",
        "Deep-sea expedition off {city} finds three new species",
        "{person} leads study linking urban trees to cooler nights",
        "Fusion test reactor sustains plasma for record 22 minutes",
        "Ancient harbour uncovered beneath {city} waterfront",
    ],
    "health": [
        "Hospitals in {city} trial AI triage to cut waiting times",
        "New vaccine shows 89% protection in late-stage trials",
        "{person} warns of early flu season as cases climb",
        "Study finds 20-minute daily walk lowers heart risk by a quarter",
        "{city} opens first 24-hour mental health walk-in centre",
    ],
    "culture": [
        "{city} film festival opens with sold-out premiere",
        "Long-lost painting rediscovered in {city} attic sells for millions",
        "Street food market transforms abandoned rail yard in {city}",
        "Orchestra's open-air concert draws record crowd to {city} park",
        "Novel set in {city} wins national book prize",
    ],
}

BREAKING = [
    "BREAKING: Major power outage hits {city} as storm makes landfall",
    "BREAKING: {person} announces resignation in televised address",
    "BREAKING: {team} owner agrees sale in surprise late-night deal",
    "BREAKING: Earthquake of magnitude 5.8 felt across {city}",
    "BREAKING: {company} halts trading after cyber attack",
    "BREAKING: Bridge closed in {city} after cracks found in main span",
]

PARAGRAPHS = [
    "Officials said the situation was being monitored closely and further updates would follow "
    "throughout the day. Residents of {city} were urged to check official channels before travelling.",
    "\"We have been preparing for this moment for months,\" said {person}, speaking to reporters on "
    "{weekday}. \"What matters now is that we deliver on what we promised.\"",
    "Analysts noted that the development could have wider implications for the region, particularly "
    "for {company}, which has significant operations in {city2}.",
    "The announcement follows weeks of speculation and comes amid growing public pressure. A recent "
    "poll found that {pct}% of respondents supported faster action.",
    "Critics, however, questioned the timeline. \"The details remain vague, and the costs have not been "
    "explained,\" one local representative said, adding that a full review would be requested.",
    "Data released on {weekday} showed a {pct}% increase compared with the same period last year, the "
    "highest figure since records began in {year}.",
    "In {city}, the mood was cautiously optimistic. Shop owners and commuters said they hoped the change "
    "would bring stability after a difficult year.",
    "Experts say the next few weeks will be crucial. \"There is still a lot we don't know,\" said "
    "{person2}. \"But the early signs are encouraging.\"",
    "The plan will be reviewed again in {month}, when a second phase is expected to be announced. "
    "Until then, existing arrangements remain in place.",
    "Supporters argue the move is long overdue. Similar measures introduced in {city2} two years ago "
    "were credited with reducing costs by roughly {pct}%.",
]

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "March", "April", "June", "September", "November"]


@dataclass
class Article:
    article_id: str
    title: str
    section: str
    category: str  # content class used for TTL: breaking | popular | regular | archive
    summary: str
    content: list[str]
    author: str
    created_at: str
    image_path: str
    thumbnail_path: str
    read_minutes: int
    tags: list[str] = field(default_factory=list)

    def card(self) -> dict:
        data = asdict(self)
        data.pop("content")
        return data


@dataclass
class SeedObject:
    object_id: str
    body: bytes
    content_type: str
    content_class: str


def _fill(template: str, rng: random.Random) -> str:
    city, city2 = rng.sample(CITIES, 2)
    company, company2 = rng.sample(COMPANIES, 2)
    team, team2 = rng.sample(TEAMS, 2)
    person, person2 = rng.sample(PEOPLE, 2)
    return template.format(
        city=city, city2=city2, company=company, company2=company2, team=team, team2=team2,
        person=person, person2=person2, coach=rng.choice([p for p in PEOPLE if "Coach" in p]),
        weekday=rng.choice(WEEKDAYS), month=rng.choice(MONTHS), pct=rng.randint(8, 74),
        year=rng.randint(1978, 2009),
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def make_article(rng: random.Random, n: int, section: str, category: str, created: datetime,
                 breaking: bool = False) -> Article:
    template = rng.choice(BREAKING if breaking else HEADLINES[section])
    title = _fill(template, rng)
    paragraphs = [_fill(p, rng) for p in rng.sample(PARAGRAPHS, rng.randint(5, 7))]
    lead = f"{title.replace('BREAKING: ', '')}. " if breaking else ""
    paragraphs[0] = lead + paragraphs[0]
    article_id = f"breaking-news-{n:03d}" if breaking else f"{section}-{n:03d}"
    words = sum(len(p.split()) for p in paragraphs)
    return Article(
        article_id=article_id,
        title=title,
        section=section,
        category=category,
        summary=paragraphs[0][:180].rsplit(" ", 1)[0] + "…",
        content=paragraphs,
        author=rng.choice(AUTHORS),
        created_at=created.isoformat(timespec="seconds"),
        image_path=f"/content/image:{article_id}",
        thumbnail_path=f"/content/thumbnail:{article_id}",
        read_minutes=max(2, words // 200 + 1),
        tags=[section] + (["breaking"] if breaking else []) + rng.sample(
            ["analysis", "local", "exclusive", "live", "explainer", "opinion", "data"], 2),
    )


def generate_articles(count: int = 60, seed: int = 50_61, now: datetime | None = None) -> list[Article]:
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    articles: list[Article] = []
    sections = list(SECTIONS)
    for i in range(1, 6):  # newest: breaking news from the last couple of hours
        created = now - timedelta(minutes=rng.randint(2, 110))
        articles.append(make_article(rng, i, rng.choice(sections), "breaking", created, breaking=True))
    for i in range(1, count - 4):
        section = sections[i % len(sections)]
        if i <= 15:
            category, created = "popular", now - timedelta(hours=rng.randint(2, 48))
        elif i <= 40:
            category, created = "regular", now - timedelta(days=rng.randint(2, 25))
        else:
            category, created = "archive", now - timedelta(days=rng.randint(30, 900))
        articles.append(make_article(rng, i + 5, section, category, created))
    articles.sort(key=lambda a: a.created_at, reverse=True)
    return articles


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render_image(article: Article, width: int, height: int, quality: int = 85) -> bytes:
    """Draw an abstract 'press photo' for an article: gradient, shapes and a section label."""
    rng = random.Random(article.article_id)
    base = SECTIONS[article.section]
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        draw.line([(0, y), (width, y)], fill=tuple(int(c * (1 - t * 0.6) + 20 * t) for c in base))
    for _ in range(14):
        r = rng.randint(width // 20, width // 4)
        x, y = rng.randint(-r, width), rng.randint(-r, height)
        tint = tuple(min(255, c + rng.randint(30, 120)) for c in base)
        draw.ellipse([x, y, x + r, y + r], fill=tint)
    img = img.filter(ImageFilter.GaussianBlur(radius=max(2, width // 120)))
    draw = ImageDraw.Draw(img)
    # Skyline silhouette so images look a little like news photography.
    x = 0
    while x < width:
        w = rng.randint(width // 30, width // 10)
        h = rng.randint(height // 8, height // 2)
        draw.rectangle([x, height - h, x + w, height], fill=(15, 18, 28))
        for wy in range(height - h + 8, height - 6, max(6, height // 40)):
            for wx in range(x + 4, x + w - 4, max(6, width // 80)):
                if rng.random() < 0.35:
                    draw.rectangle([wx, wy, wx + 2, wy + 3], fill=(250, 220, 140))
        x += w + rng.randint(0, width // 60)
    label = article.section.upper() + ("  •  BREAKING" if article.category == "breaking" else "")
    font = _font(max(12, height // 14))
    pad = height // 30
    box = draw.textbbox((0, 0), label, font=font)
    draw.rectangle([pad, pad, pad * 3 + box[2], pad * 3 + box[3]], fill=(0, 0, 0))
    draw.text((pad * 2, pad * 2), label, font=font, fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def cached_image(article: Article, width: int, height: int, quality: int = 85) -> bytes:
    path = IMAGE_CACHE_DIR / f"{article.article_id}-{article.category}-{article.section}-{width}.jpg"
    if path.exists():
        return path.read_bytes()
    return render_image(article, width, height, quality)


def prerender_images(count: int = 60) -> None:
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for a in generate_articles(count):
        for width, height, quality in ((1200, 675, 85), (400, 225, 80)):
            path = IMAGE_CACHE_DIR / f"{a.article_id}-{a.category}-{a.section}-{width}.jpg"
            path.write_bytes(render_image(a, width, height, quality))


def build_index(articles: list[Article]) -> bytes:
    payload = {
        "paper": "The Daily Edge",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": list(SECTIONS),
        "articles": [a.card() for a in articles],
    }
    return json.dumps(payload).encode()


def article_objects(article: Article) -> list[SeedObject]:
    return [
        SeedObject(f"article:{article.article_id}", json.dumps(asdict(article)).encode(),
                   "application/json", article.category),
        SeedObject(f"image:{article.article_id}", cached_image(article, 1200, 675),
                   "image/jpeg", "image"),
        SeedObject(f"thumbnail:{article.article_id}", cached_image(article, 400, 225, 80),
                   "image/jpeg", "thumbnail"),
    ]


STATIC_TYPES = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}


def static_objects() -> list[SeedObject]:
    return [
        SeedObject(f"static:{p.name}", p.read_bytes(), STATIC_TYPES.get(p.suffix, "application/octet-stream"),
                   "static")
        for p in sorted(STATIC_DIR.iterdir()) if p.is_file()
    ]


def seed_version() -> str:
    import hashlib

    h = hashlib.sha256(GENERATOR_VERSION.encode())
    for p in sorted(STATIC_DIR.iterdir()):
        if p.is_file():
            h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest()[:16]


def generate_all(count: int = 60) -> tuple[list[Article], list[SeedObject]]:
    articles = generate_articles(count)
    objects: list[SeedObject] = []
    for article in articles:
        objects.extend(article_objects(article))
    objects.extend(static_objects())
    objects.append(SeedObject("index:latest", build_index(articles), "application/json", "index"))
    return articles, objects
