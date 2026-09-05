import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from xml.sax.saxutils import escape


# ============================================================
# CONFIGURATION
# ============================================================

SOURCE_URL = "https://www.defense.gouv.fr/actualites"
BASE_URL = "https://www.defense.gouv.fr"

FEED_TITLE = "Ministère des Armées - Actualités"
FEED_DESCRIPTION = (
    "Actualités du Ministère des Armées et des Anciens combattants"
)

MAX_ARTICLES = 30

# User-Agent réaliste
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# ============================================================
# SESSION HTTP ROBUSTE
# ============================================================

def create_session():
    session = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(HEADERS)

    return session


# ============================================================
# RÉCUPÉRATION DE LA PAGE
# ============================================================

def fetch_page(session, url, attempts=3):
    """
    Timeout séparé :
      - 15 secondes pour établir la connexion
      - 45 secondes pour recevoir les données

    Plusieurs tentatives avec délai progressif.
    """

    for attempt in range(1, attempts + 1):

        try:
            print(
                f"Téléchargement de {url} "
                f"(tentative {attempt}/{attempts})..."
            )

            response = session.get(
                url,
                timeout=(15, 45),
                allow_redirects=True,
            )

            response.raise_for_status()

            print(
                f"Page récupérée : "
                f"{len(response.content):,} octets"
            )

            return response

        except requests.exceptions.RequestException as exc:

            print(
                f"Erreur lors de la récupération : {exc}"
            )

            if attempt < attempts:
                delay = 3 * attempt
                print(
                    f"Nouvelle tentative dans {delay} secondes..."
                )
                time.sleep(delay)

    raise RuntimeError(
        f"Impossible de récupérer {url} "
        f"après {attempts} tentatives."
    )


# ============================================================
# OUTILS
# ============================================================

MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}


def parse_french_date(text):
    """
    Transforme :
        04 septembre 2026
    en datetime UTC.

    Si aucune date n'est trouvée, retourne None.
    """

    if not text:
        return None

    pattern = (
        r"\b(\d{1,2})\s+"
        r"(janvier|février|fevrier|mars|avril|mai|juin|"
        r"juillet|août|aout|septembre|octobre|novembre|"
        r"décembre|decembre)\s+"
        r"(\d{4})\b"
    )

    match = re.search(
        pattern,
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS[match.group(2).lower()]
    year = int(match.group(3))

    try:
        return datetime(
            year,
            month,
            day,
            12,
            0,
            0,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def clean_text(text):
    if not text:
        return ""

    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def absolute_url(url):
    if not url:
        return ""

    return urljoin(BASE_URL, url.strip())


def get_image_from_container(container):
    """
    Cherche l'image principale dans le bloc de l'article.
    """

    if not container:
        return ""

    # 1. Image classique
    img = container.find("img")

    if img:

        # Priorité aux attributs pouvant contenir une vraie URL
        for attribute in [
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
        ]:
            value = img.get(attribute)

            if value and not value.startswith("data:"):
                return absolute_url(value)

        # 2. srcset
        srcset = img.get("srcset")

        if srcset:
            candidates = [
                item.strip().split(" ")[0]
                for item in srcset.split(",")
                if item.strip()
            ]

            if candidates:
                return absolute_url(candidates[-1])

    # 3. Image en background CSS
    for element in container.find_all(style=True):

        style = element.get("style", "")

        match = re.search(
            r"url\(['\"]?([^'\")]+)['\"]?\)",
            style,
            flags=re.IGNORECASE,
        )

        if match:
            return absolute_url(match.group(1))

    return ""


def get_article_container(heading):
    """
    À partir du titre H2/H3, remonte dans le DOM
    pour trouver le bloc correspondant à une actualité.
    """

    current = heading

    for _ in range(6):

        current = current.parent

        if current is None:
            break

        text = clean_text(
            current.get_text(" ", strip=True)
        )

        # On cherche un bloc raisonnable contenant
        # le titre + une date.
        if len(text) > 50 and len(text) < 5000:

            if re.search(
                r"\b\d{1,2}\s+"
                r"(janvier|février|fevrier|mars|avril|mai|juin|"
                r"juillet|août|aout|septembre|octobre|novembre|"
                r"décembre|decembre)\s+\d{4}\b",
                text,
                flags=re.IGNORECASE,
            ):
                return current

    return heading.parent


# ============================================================
# EXTRACTION DES ARTICLES
# ============================================================

def extract_articles(html):

    soup = BeautifulSoup(html, "html.parser")

    articles = []
    seen_urls = set()

    # La page utilise actuellement des titres de niveau H2/H3
    # pour les actualités.
    headings = soup.find_all(["h2", "h3"])

    print(f"{len(headings)} titres H2/H3 trouvés.")

    for heading in headings:

        title = clean_text(
            heading.get_text(" ", strip=True)
        )

        if not title:
            continue

        # Ignore les titres trop courts qui correspondent
        # probablement à la navigation.
        if len(title) < 20:
            continue

        container = get_article_container(heading)

        if not container:
            continue

        # ----------------------------------------------------
        # URL
        # ----------------------------------------------------

        link = None

        # D'abord, on cherche un lien dans le titre.
        title_link = heading.find("a", href=True)

        if title_link:
            link = title_link.get("href")

        # Sinon, recherche dans le bloc.
        if not link:
            block_link = container.find(
                "a",
                href=True,
            )

            if block_link:
                link = block_link.get("href")

        if not link:
            continue

        link = absolute_url(link)

        # On ne conserve que les pages du site.
        if not link.startswith(BASE_URL):
            continue

        # On exclut les liens génériques.
        if link.rstrip("/") == SOURCE_URL.rstrip("/"):
            continue

        # Déduplication.
        if link in seen_urls:
            continue

        # ----------------------------------------------------
        # DATE
        # ----------------------------------------------------

        container_text = clean_text(
            container.get_text(" ", strip=True)
        )

        published = parse_french_date(
            container_text
        )

        if not published:
            # Si aucune date n'est trouvée, ce n'est
            # probablement pas une actualité.
            continue

        # ----------------------------------------------------
        # IMAGE
        # ----------------------------------------------------

        image_url = get_image_from_container(
            container
        )

        # ----------------------------------------------------
        # DESCRIPTION
        # ----------------------------------------------------

        description = ""

        # On cherche les paragraphes du bloc.
        paragraphs = container.find_all("p")

        for paragraph in paragraphs:

            candidate = clean_text(
                paragraph.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                len(candidate) > 40
                and candidate != title
            ):
                description = candidate
                break

        # Fallback : texte du bloc
        if not description:

            text = container_text

            # Retire le titre et la date.
            description = text.replace(
                title,
                "",
                1,
            )

            if published:
                date_string = published.strftime(
                    "%d/%m/%Y"
                )
                description = description.replace(
                    date_string,
                    "",
                )

            description = clean_text(
                description
            )

        # Limite raisonnable pour le RSS.
        description = description[:1500]

        articles.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "image": image_url,
                "published": published,
            }
        )

        seen_urls.add(link)

        print(
            f"ARTICLE : {title}"
        )

        print(
            f"  date   : {published.date()}"
        )

        print(
            f"  image  : {image_url or 'AUCUNE'}"
        )

        # On récupère suffisamment d'articles.
        if len(articles) >= MAX_ARTICLES:
            break

    # Tri chronologique décroissant.
    articles.sort(
        key=lambda article: article["published"],
        reverse=True,
    )

    return articles[:MAX_ARTICLES]


# ============================================================
# RSS
# ============================================================

def xml_escape(value):
    return escape(
        str(value or ""),
        {
            '"': "&quot;",
            "'": "&apos;",
        },
    )


def build_description(article):
    """
    Description HTML destinée notamment à Inoreader.

    L'image est placée directement dans la description.
    """

    description = clean_text(
        article["description"]
    )

    image = article["image"]

    html = ""

    if image:
        html += (
            '<p>'
            f'<img src="{xml_escape(image)}" '
            'alt="" '
            'style="max-width:100%;height:auto;" />'
            '</p>'
        )

    if description:
        html += (
            f"<p>{xml_escape(description)}</p>"
        )

    html += (
        '<p>'
        f'<a href="{xml_escape(article["link"])}">'
        "Lire l'article complet sur defense.gouv.fr"
        "</a>"
        "</p>"
    )

    return html


def build_rss(articles):

    now = datetime.now(timezone.utc)

    items = []

    for article in articles:

        title = xml_escape(
            article["title"]
        )

        link = xml_escape(
            article["link"]
        )

        description = build_description(
            article
        )

        published = article["published"]

        pub_date = format_datetime(
            published
        )

        # Image éventuelle
        enclosure = ""

        if article["image"]:

            enclosure = (
                f'<enclosure '
                f'url="{xml_escape(article["image"])}" '
                f'type="image/jpeg" />'
            )

        # GUID = URL de l'article.
        # Il reste stable entre deux exécutions.
        guid = xml_escape(
            article["link"]
        )

        items.append(
            f"""
    <item>
      <title>{title}</title>

      <link>{link}</link>

      <guid isPermaLink="true">{guid}</guid>

      <pubDate>{pub_date}</pubDate>

      <description><![CDATA[
{description}
      ]]></description>

      {enclosure}
    </item>
"""
        )

    last_build = format_datetime(now)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>

<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/">

  <channel>

    <title>{xml_escape(FEED_TITLE)}</title>

    <link>{SOURCE_URL}</link>

    <description>{xml_escape(FEED_DESCRIPTION)}</description>

    <language>fr-fr</language>

    <ttl>60</ttl>

    <lastBuildDate>{last_build}</lastBuildDate>

    {''.join(items)}

  </channel>

</rss>
"""

    return rss


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    print("==========================================")
    print(" Génération du flux RSS Defense.fr")
    print("==========================================")

    session = create_session()

    try:

        response = fetch_page(
            session,
            SOURCE_URL,
        )

    except Exception as exc:

        print(
            f"ERREUR FATALE : {exc}"
        )

        raise

    articles = extract_articles(
        response.text
    )

    if not articles:

        raise RuntimeError(
            "Aucun article détecté. "
            "Le format de la page a peut-être changé."
        )

    print()
    print(
        f"{len(articles)} articles détectés."
    )

    rss = build_rss(
        articles
    )

    with open(
        "feed.xml",
        "w",
        encoding="utf-8",
    ) as file:

        file.write(rss)

    print()
    print(
        "feed.xml généré avec succès."
    )


if __name__ == "__main__":
    main()
