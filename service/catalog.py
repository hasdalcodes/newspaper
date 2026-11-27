"""Built-in catalog of well-known news RSS/Atom feeds.

`seen` = the URL appeared in published feed lists / the publisher's own RSS page when this catalog was
compiled. Feeds change without notice, so the UI also offers a live "Check" before you add them.
CNN and Reuters are deliberately absent: CNN's public feeds stopped updating in 2023 and Reuters has no
official public RSS.
"""


def _f(id, name, group, lang, url, seen=True, starter=False):
    return {"id": id, "name": name, "group": group, "lang": lang, "url": url, "seen": seen, "starter": starter}


W, US, ME, BIZ, TECH, SPORT, OTHER = ("World news (English)", "United States", "Middle East & Arabic", "Business",
                                      "Technology", "Sport", "Other languages")

CATALOG = [
    # ---- World news (English)
    _f("aljazeera-en", "Al Jazeera English", W, "en", "https://www.aljazeera.com/xml/rss/all.xml", starter=True),
    _f("bbc-top", "BBC News - Top stories", W, "en", "https://feeds.bbci.co.uk/news/rss.xml"),
    _f("bbc-world", "BBC News - World", W, "en", "https://feeds.bbci.co.uk/news/world/rss.xml", starter=True),
    _f("bbc-me", "BBC News - Middle East", W, "en", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"),
    _f("bbc-africa", "BBC News - Africa", W, "en", "https://feeds.bbci.co.uk/news/world/africa/rss.xml"),
    _f("bbc-asia", "BBC News - Asia", W, "en", "https://feeds.bbci.co.uk/news/world/asia/rss.xml"),
    _f("bbc-europe", "BBC News - Europe", W, "en", "https://feeds.bbci.co.uk/news/world/europe/rss.xml"),
    _f("guardian-world", "The Guardian - World", W, "en", "https://www.theguardian.com/world/rss", starter=True),
    _f("dw-en", "Deutsche Welle (English)", W, "en", "http://rss.dw.com/rdf/rss-en-all", starter=True),
    _f("france24-en", "France 24 (English)", W, "en", "https://www.france24.com/en/rss", seen=False),
    _f("sky-world", "Sky News - World", W, "en", "https://feeds.skynews.com/feeds/rss/world.xml", seen=False),
    _f("economist-week", "The Economist - The world this week", W, "en",
       "https://www.economist.com/the-world-this-week/rss.xml"),
    _f("semafor", "Semafor", W, "en", "https://www.semafor.com/rss.xml"),
    # ---- United States
    _f("npr", "NPR News", US, "en", "https://feeds.npr.org/1001/rss.xml", starter=True),
    _f("nyt-home", "The New York Times - Home", US, "en", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    _f("nyt-world", "The New York Times - World", US, "en", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    _f("wapo-world", "The Washington Post - World", US, "en", "https://feeds.washingtonpost.com/rss/world"),
    _f("nbc", "NBC News - Top stories", US, "en", "http://feeds.nbcnews.com/feeds/topstories"),
    _f("pbs-politics", "PBS NewsHour - Politics", US, "en", "https://www.pbs.org/newshour/feeds/rss/politics"),
    _f("politico", "Politico - Picks", US, "en", "https://www.politico.com/rss/politicopicks.xml"),
    _f("propublica", "ProPublica", US, "en", "https://www.propublica.org/feeds/propublica/main"),
    # ---- Middle East & Arabic
    _f("aljazeera-ar", "Al Jazeera Arabic (aljazeera.net)", ME, "ar",
       "https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/"
       "73d0e1b4-532f-45ef-b135-bfdff8b8cab9", seen=False),
    _f("france24-ar", "France 24 Arabic", ME, "ar", "https://www.france24.com/ar/top-stories/rss"),
    _f("bbc-ar", "BBC Arabic", ME, "ar", "https://feeds.bbci.co.uk/arabic/rss.xml", seen=False),
    _f("dw-ar", "Deutsche Welle Arabic", ME, "ar", "https://rss.dw.com/xml/rss-ar-all", seen=False),
    _f("sky-ar", "Sky News Arabia", ME, "ar", "https://www.skynewsarabia.com/rss.xml"),
    _f("masry", "Al-Masry Al-Youm (Egypt)", ME, "ar", "https://www.almasryalyoum.com/rss/rssfeed"),
    _f("dostor", "Al-Dostor (Egypt)", ME, "ar", "https://www.dostor.org/rss.aspx"),
    _f("newarab", "The New Arab", ME, "ar", "https://www.newarab.com/rss"),
    _f("alsharq", "Al-Sharq (Qatar)", ME, "ar", "https://al-sharq.com/rss/latestNews"),
    _f("okaz", "Okaz (Saudi Arabia)", ME, "ar", "https://www.okaz.com.sa/rssFeed/0"),
    _f("albawaba-ar", "Al Bawaba (Arabic)", ME, "ar", "https://www.albawaba.com/rss/ar-all"),
    # ---- Business
    _f("bbc-business", "BBC News - Business", BIZ, "en", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    _f("economist-latest", "The Economist - Latest", BIZ, "en", "https://www.economist.com/latest/rss.xml"),
    _f("bloomberg-markets", "Bloomberg - Markets", BIZ, "en", "https://feeds.bloomberg.com/markets/news.rss"),
    _f("wsj-markets", "Wall Street Journal - Markets", BIZ, "en", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    # ---- Technology
    _f("bbc-tech", "BBC News - Technology", TECH, "en", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    _f("verge", "The Verge", TECH, "en", "https://www.theverge.com/rss/index.xml"),
    _f("hn", "Hacker News - Front page", TECH, "en", "https://news.ycombinator.com/rss"),
    _f("ars", "Ars Technica", TECH, "en", "https://feeds.arstechnica.com/arstechnica/index", seen=False),
    # ---- Sport
    _f("bbc-sport", "BBC Sport", SPORT, "en", "https://feeds.bbci.co.uk/sport/rss.xml"),
    _f("espn", "ESPN - Top news", SPORT, "en", "https://www.espn.com/espn/rss/news"),
    # ---- Other languages
    _f("bbc-mundo", "BBC Mundo (Spanish)", OTHER, "es", "https://feeds.bbci.co.uk/mundo/rss.xml"),
    _f("bbc-brasil", "BBC Brasil (Portuguese)", OTHER, "pt", "https://feeds.bbci.co.uk/portuguese/rss.xml"),
    _f("bbc-ukrainian", "BBC News Ukrainian", OTHER, "uk", "https://feeds.bbci.co.uk/ukrainian/rss.xml"),
]

BY_ID = {c["id"]: c for c in CATALOG}
STARTER_IDS = [c["id"] for c in CATALOG if c["starter"]]
assert len(BY_ID) == len(CATALOG), "duplicate catalog id"


def norm(url: str) -> str:
    return url.strip().rstrip("/").lower().replace("http://", "https://")
