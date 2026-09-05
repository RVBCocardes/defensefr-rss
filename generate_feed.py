import requests
from bs4 import BeautifulSoup
from email.utils import format_datetime
from datetime import datetime, timezone
from xml.sax.saxutils import escape

SOURCE_URL = "https://www.defense.gouv.fr/actualites"
FEED_URL = "https://RVBCocardes.github.io/defense-rss/feed.xml"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DefenseRSS/1.0)"
}

response = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
response.raise_for_status()

soup = BeautifulSoup(response.text, "html.parser")

articles = []

# Les actualités sont présentées sous forme de liens vers les articles.
# On récupère les liens internes qui ressemblent à des articles.
for link in soup.find_all("a", href=True):

    title = link.get_text(" ", strip=True)
    href = link["href"]

    if not title or len(title) < 15:
        continue

    if href.startswith("/"):
        href = "https://www.defense.gouv.fr" + href

    if not href.startswith("https://www.defense.gouv.fr/"):
        continue

    # On évite les liens de navigation et les doublons.
    if href.rstrip("/") == SOURCE_URL.rstrip("/"):
        continue

    if href in [a["link"] for a in articles]:
        continue

    # On essaie de récupérer le bloc contenant le résumé et la date.
    parent = link.find_parent(["article", "div", "li"])

    description = ""
    date_text = ""

    if parent:
        text = parent.get_text(" ", strip=True)

        # Recherche d'une date française dans le bloc.
        import re

        date_match = re.search(
            r"\b(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|"
            r"juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})\b",
            text,
            re.IGNORECASE
        )

        if date_match:
            date_text = date_match.group(0)

        # Le texte autour du titre servira de description.
        description = text[:1000]

    articles.append({
        "title": title,
        "link": href,
        "description": description,
        "date": date_text
    })

    if len(articles) >= 30:
        break


# Nettoyage des doublons par titre
unique = []
seen = set()

for article in articles:
    key = article["title"].lower()

    if key not in seen:
        seen.add(key)
        unique.append(article)

articles = unique[:30]


def rss_date():
    return format_datetime(datetime.now(timezone.utc))


items = []

for article in articles:

    title = escape(article["title"])
    link = escape(article["link"])
    description = escape(article["description"])

    items.append(f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <description>{description}</description>
      <pubDate>{rss_date()}</pubDate>
    </item>
    """)

rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Ministère des Armées - Actualités</title>
    <link>{SOURCE_URL}</link>
    <description>Actualités du Ministère des Armées et des Anciens combattants</description>
    <language>fr-fr</language>
    <lastBuildDate>{rss_date()}</lastBuildDate>
    {''.join(items)}
  </channel>
</rss>
"""

with open("feed.xml", "w", encoding="utf-8") as f:
    f.write(rss)

print(f"Flux RSS généré avec {len(articles)} articles.")
