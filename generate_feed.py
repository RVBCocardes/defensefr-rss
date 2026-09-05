import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from html import unescape
from urllib.parse import urljoin, urlparse

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

MAX_ARTICLES = 25

# Nombre de liens d'articles à récupérer sur la page liste
MAX_CANDIDATES = 40

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,"
        "*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


# ============================================================
# SESSION HTTP
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
        pool_connections=5,
        pool_maxsize=5,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)

    return session


def fetch(session, url, attempts=3):
    """
    Téléchargement robuste.

    timeout:
      - 15 secondes pour la connexion
      - 45 secondes pour la lecture

    Un échec sur un article ne fera pas échouer tout le flux.
    """

    for attempt in range(1, attempts + 1):

        try:
            print(
                f"GET {url} "
                f"(tentative {attempt}/{attempts})"
            )

            response = session.get(
                url,
                timeout=(15, 45),
                allow_redirects=True,
            )

            response.raise_for_status()

            return response

        except requests.exceptions.RequestException as exc:

            print(f"  ERREUR : {exc}")

            if attempt < attempts:
                delay = attempt * 3
                print(
                    f"  Nouvelle tentative dans "
                    f"{delay} secondes..."
                )
                time.sleep(delay)

    return None


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


def clean_text(value):
    if not value:
        return ""

    value = unescape(value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def absolute_url(value):
    if not value:
        return ""

    return urljoin(BASE_URL, value.strip())


def parse_french_date(value):
    """
    Exemples acceptés :

      Publié le : 12 juin 2026
      12 juin 2026
    """

    if not value:
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
        value,
        re.IGNORECASE,
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


def parse_iso_date(value):
    if not value:
        return None

    value = value.strip()

    try:
        # 2026-06-12T12:00:00+00:00
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(timezone.utc)

    except ValueError:
        return None


# ============================================================
# DÉTECTION DES VRAIS ARTICLES
# ============================================================

def extract_article_links(html):

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    candidates = []
    seen = set()

    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = absolute_url(
            link.get("href")
        )

        parsed = urlparse(href)

        # Domaine exact
        if parsed.netloc.lower() != "www.defense.gouv.fr":
            continue

        path = parsed.path.rstrip("/")

        # ----------------------------------------------------
        # IMPORTANT :
        #
        # On veut les pages du type :
        #
        # /actualites/mon-article
        #
        # mais PAS :
        #
        # /actualites
        # /actualites/une-rubrique
        #
        # ni :
        #
        # /terre/actualites/...
        # /air/actualites/...
        #
        # puisque notre source est la rubrique générale.
        # ----------------------------------------------------

        if not path.startswith("/actualites/"):
            continue

        slug = path[len("/actualites/"):].strip("/")

        if not slug:
            continue

        # Évite les URLs contenant une nouvelle sous-rubrique
        if "/" in slug:
            continue

        # Exclusion de quelques chemins manifestement techniques
        forbidden = [
            "recherche",
            "contact",
            "mentions-legales",
            "accessibilite",
            "plan-du-site",
        ]

        if slug.lower() in forbidden:
            continue

        if href in seen:
            continue

        title = clean_text(
            link.get_text(" ", strip=True)
        )

        if len(title) < 15:
            continue

        candidates.append(
            {
                "url": href,
                "link_title": title,
            }
        )

        seen.add(href)

        if len(candidates) >= MAX_CANDIDATES:
            break

    print(
        f"{len(candidates)} liens "
        f"/actualites/... candidats trouvés."
    )

    return candidates


# ============================================================
# EXTRACTION D'UNE PAGE D'ARTICLE
# ============================================================

def get_meta(soup, *names):

    for name in names:

        # <meta property="og:title" ...>
        tag = soup.find(
            "meta",
            attrs={
                "property": name
            },
        )

        if tag and tag.get("content"):
            return clean_text(
                tag.get("content")
            )

        # <meta name="description" ...>
        tag = soup.find(
            "meta",
            attrs={
                "name": name
            },
        )

        if tag and tag.get("content"):
            return clean_text(
                tag.get("content")
            )

    return ""


def get_image(soup):

    # OpenGraph = priorité
    image = get_meta(
        soup,
        "og:image",
        "twitter:image",
    )

    if image:
        return absolute_url(image)

    # Fallback : première image avec src
    for img in soup.find_all(
        "img",
        src=True,
    ):

        src = img.get("src", "")

        if src and not src.startswith("data:"):
            return absolute_url(src)

    return ""


def extract_date(soup):

    # 1. OpenGraph / métadonnées
    for meta_name in [
        "article:published_time",
        "datePublished",
        "date",
    ]:

        value = get_meta(
            soup,
            meta_name,
        )

        parsed = parse_iso_date(value)

        if parsed:
            return parsed

    # 2. Balise <time>
    for time_tag in soup.find_all("time"):

        value = (
            time_tag.get("datetime")
            or time_tag.get_text(
                " ",
                strip=True,
            )
        )

        parsed = (
            parse_iso_date(value)
            or parse_french_date(value)
        )

        if parsed:
            return parsed

    # 3. Recherche dans le texte
    text = clean_text(
        soup.get_text(
            " ",
            strip=True,
        )
    )

    return parse_french_date(text)


def extract_title(soup, fallback):

    # OpenGraph
    title = get_meta(
        soup,
        "og:title",
    )

    if title:
        return title

    # H1
    h1 = soup.find("h1")

    if h1:
        title = clean_text(
            h1.get_text(
                " ",
                strip=True,
            )
        )

        if title:
            return title

    return fallback


def extract_description(soup, title):

    # --------------------------------------------------------
    # OpenGraph description
    # --------------------------------------------------------

    description = get_meta(
        soup,
        "og:description",
        "description",
    )

    if description:
        return description[:2000]

    # --------------------------------------------------------
    # On cherche ensuite le premier paragraphe
    # significatif après le H1.
    # --------------------------------------------------------

    h1 = soup.find("h1")

    if h1:

        # Regarde les éléments suivants
        for element in h1.find_all_next(
            ["p", "div"],
            limit=30,
        ):

            text = clean_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            if text == title:
                continue

            # Ignore les métadonnées
            if (
                "Publié le :" in text
                or "Direction :" in text
            ):
                continue

            # Évite les textes minuscules de navigation
            if len(text) < 50:
                continue

            return text[:2000]

    return ""


def extract_article(
    session,
    candidate,
):

    url = candidate["url"]

    response = fetch(
        session,
        url,
        attempts=2,
    )

    if response is None:
        print(
            "  -> article ignoré"
        )
        return None

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    title = extract_title(
        soup,
        candidate["link_title"],
    )

    published = extract_date(
        soup
    )

    description = extract_description(
        soup,
        title,
    )

    image = get_image(
        soup
    )

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    if not title:
        print(
            "  -> titre absent : ignoré"
        )
        return None

    if not published:
        print(
            f"  -> date absente : {title}"
        )
        return None

    print(
        f"  ARTICLE : {title}"
    )

    print(
        f"    date : {published.strftime('%d/%m/%Y')}"
    )

    print(
        f"    image : "
        f"{image if image else 'AUCUNE'}"
    )

    print(
        f"    description : "
        f"{description[:120]}..."
        if description
        else
        "    description : AUCUNE"
    )

    return {
        "title": title,
        "link": url,
        "description": description,
        "image": image,
        "published": published,
    }


# ============================================================
# RSS
# ============================================================

def xml_escape(value):
    return escape(
        str(value or "")
    )


def build_description(article):

    parts = []

    image = article["image"]

    if image:

        parts.append(
            "<p>"
            f'<img src="{xml_escape(image)}" '
            'alt="" '
            'style="max-width:100%;height:auto;" />'
            "</p>"
        )

    description = article["description"]

    if description:

        parts.append(
            "<p>"
            f"{xml_escape(description)}"
            "</p>"
        )

    parts.append(
        "<p>"
        f'<a href="{xml_escape(article["link"])}">'
        "Lire l'article complet sur defense.gouv.fr"
        "</a>"
        "</p>"
    )

    return "\n".join(parts)


def build_rss(articles):

    now = datetime.now(
        timezone.utc
    )

    items = []

    for article in articles:

        title = xml_escape(
            article["title"]
        )

        link = xml_escape(
            article["link"]
        )

        guid = xml_escape(
            article["link"]
        )

        pub_date = format_datetime(
            article["published"]
        )

        description = build_description(
            article
        )

        enclosure = ""

        if article["image"]:

            # On ne prétend pas connaître
            # exactement le type MIME.
            # image/jpeg fonctionne avec la
            # majorité des images du site.
            enclosure = (
                "<enclosure "
                f'url="{xml_escape(article["image"])}" '
                'type="image/jpeg" />'
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

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>

<rss version="2.0"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">

  <channel>

    <title>{xml_escape(FEED_TITLE)}</title>

    <link>{SOURCE_URL}</link>

    <description>{xml_escape(FEED_DESCRIPTION)}</description>

    <language>fr-fr</language>

    <ttl>60</ttl>

    <lastBuildDate>{format_datetime(now)}</lastBuildDate>

    {''.join(items)}

  </channel>

</rss>
"""

    return rss


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    print()
    print("=" * 60)
    print(" MINISTÈRE DES ARMÉES - GÉNÉRATION RSS")
    print("=" * 60)
    print()

    session = create_session()

    # --------------------------------------------------------
    # 1. Page des actualités
    # --------------------------------------------------------

    response = fetch(
        session,
        SOURCE_URL,
        attempts=3,
    )

    if response is None:

        raise RuntimeError(
            "Impossible de récupérer la page "
            "des actualités après plusieurs tentatives."
        )

    # --------------------------------------------------------
    # 2. Recherche des vrais liens /actualites/slug
    # --------------------------------------------------------

    candidates = extract_article_links(
        response.text
    )

    if len(candidates) < 5:

        raise RuntimeError(
            "Trop peu de vrais articles détectés "
            f"({len(candidates)}). "
            "Le flux précédent n'est PAS remplacé."
        )

    # --------------------------------------------------------
    # 3. Ouverture des articles
    # --------------------------------------------------------

    articles = []

    for candidate in candidates:

        article = extract_article(
            session,
            candidate,
        )

        if article:

            articles.append(
                article
            )

        # On s'arrête dès qu'on a assez
        # d'articles valides.
        if len(articles) >= MAX_ARTICLES:
            break

        # Petite pause pour ne pas marteler
        # le serveur.
        time.sleep(0.5)

    # --------------------------------------------------------
    # 4. Validation finale
    # --------------------------------------------------------

    if len(articles) < 5:

        raise RuntimeError(
            "Moins de 5 articles valides "
            f"ont été récupérés ({len(articles)}). "
            "Le flux précédent n'est PAS remplacé."
        )

    # Tri du plus récent au plus ancien
    articles.sort(
        key=lambda item: item["published"],
        reverse=True,
    )

    articles = articles[
        :MAX_ARTICLES
    ]

    print()
    print(
        f"{len(articles)} articles valides."
    )

    # --------------------------------------------------------
    # 5. Génération du RSS
    # --------------------------------------------------------

    rss = build_rss(
        articles
    )

    # IMPORTANT :
    # feed.xml n'est écrit qu'après validation.
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

    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
