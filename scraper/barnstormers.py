"""Scraper for Aviat Husky aircraft listings on barnstormers.com.

Listings come from a Barnstormers advanced search on the ad headline
"Husky". Only whole-aircraft-for-sale listings are published: each ad's
title must name a Husky and no other airplane make, and titles that look like
parts/accessories/services/raffles are dropped. Surviving titles are
rewritten to a canonical "YEAR Aviat Husky MODEL" form so every listing
follows the same format.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .common import (
    MIN_AIRCRAFT_PRICE,
    Listing,
    extract_date,
    extract_location,
    extract_price,
    fetch,
    format_aircraft_title,
    price_value,
)

SITE_NAME = "Barnstormers.com"
BASE = "https://www.barnstormers.com"
MAKE = "Aviat"

# Barnstormers advanced search for ads whose headline contains "Husky".
SEARCH_URLS = [
    f"{BASE}/cat_search.php?headline=Husky&body=&part_num=&mfg=&model="
    "&user__profile__company=&user__last_name=&user__first_name="
    "&user__profile__country=&specialcase__state=&user__profile__city="
    "&user__profile__uzip=&specialcase__phone=&user__email=&my_cats__name="
    "&price__gte=&price__lte=&search_type=advanced&keyword=",
]

MAX_PAGES = 15
LISTING_LINK_RE = re.compile(r"^/classified-(\d+)-(.+)\.html$")
GENERIC_SITE_TITLE_SNIPPET = "barnstormers.com find aircraft"

_HUSKY_RE = re.compile(r"husky", re.IGNORECASE)
# A-1 model code with its variant letter (A-1, A-1A, A-1B, A-1C) and, for the
# A-1C, its engine suffix (A-1C-180, A-1C-200). Titles come from URL slugs,
# whose hyphens read as spaces ("Husky A 1", "Husky A1 B"), so any spacing is
# accepted right after "Husky"; elsewhere the letter is required ("A1C200"),
# since a bare "A1" is usually "A1 condition".
_MODEL_RE = re.compile(
    r"husky\s?-?\s?a\s?-?\s?1(?:\s?-?\s?([abc]))?(?:\s?-?\s?(180|200))?(?!\d)(?![a-z])"
    r"|\ba\s?-?\s?1\s?-?\s?([abc])(?:\s?-?\s?(180|200))?(?!\d)(?![a-z])",
    re.IGNORECASE,
)
# Other makes. A "Husky" title naming one of these is a comparison, a part
# ("Husky tailwheel for Super Cub"), or some other airplane, and is dropped.
_OTHER_MAKE_RE = re.compile(
    r"\bcub\b|super\s*cub|cessna|piper|kitfox|citabria|champ|maule|rans\b|highlander|savage|scout",
    re.IGNORECASE,
)


def _extract_model(title: str) -> tuple[str, str] | None:
    if not _HUSKY_RE.search(title) or _OTHER_MAKE_RE.search(title):
        return None
    match = _MODEL_RE.search(title)
    if not match:
        return MAKE, "Husky"
    letter = match.group(1) or match.group(3) or ""
    engine = match.group(2) or match.group(4)
    model = "A-1" + letter.upper() + (f"-{engine}" if engine else "")
    return MAKE, f"Husky {model}"


def _title_from_url(url: str) -> str:
    """Listing pages share a generic <title>/<h1>, but the URL slug is the ad's own title."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = LISTING_LINK_RE.match("/" + slug)
    if not match:
        return unquote(slug)
    return unquote(match.group(2)).replace("-", " ").strip()


def _find_listing_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = urlparse(urljoin(BASE, a["href"])).path
        if LISTING_LINK_RE.match(href):
            links.add(urljoin(BASE, href))
    return links


def _page_url(search_url: str, page: int) -> str:
    """Build a search results page's URL directly by setting its page=<n>
    query parameter, rather than hunting for "Next" links - Barnstormers'
    pager renders as bare page-number buttons with no "Next" text or
    rel="next" for a link-following heuristic to find.
    """
    if page <= 1:
        return search_url
    parts = urlparse(search_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    query.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _debug_dump_hrefs(html: str, limit: int = 25) -> None:
    soup = BeautifulSoup(html, "lxml")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    interesting = [h for h in hrefs if "classified" in h.lower() or "husky" in h.lower()]
    sample = interesting[:limit] or hrefs[:limit]
    print(f"  [debug] {len(hrefs)} total <a href> on page; sample: {sample}")


def _parse_detail_page(url: str, html: str) -> Listing | None:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        title = re.sub(r"\s*[\|\-]\s*Barnstormers.*$", "", title, flags=re.IGNORECASE).strip()
    if not title or GENERIC_SITE_TITLE_SNIPPET in title.lower():
        title = _title_from_url(url)
    if not title:
        return None

    text = soup.get_text(" ", strip=True)

    formatted_title = format_aircraft_title(title, text, _extract_model)
    if not formatted_title:
        return None
    title = formatted_title

    price = extract_price(text)
    value = price_value(price)
    if value is not None and value < MIN_AIRCRAFT_PRICE:
        return None
    location = extract_location(text)
    date_posted = extract_date(text)

    return Listing(
        title=title,
        price=price,
        location=location,
        date_posted=date_posted,
        site=SITE_NAME,
        url=url,
    )


def scrape() -> list[Listing]:
    print(f"[{SITE_NAME}] starting scrape")
    all_links: set[str] = set()

    for search_url in SEARCH_URLS:
        seen_this_search: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            url = _page_url(search_url, page)
            html = fetch(url)
            if not html:
                break
            links = _find_listing_links(html)
            new_links = links - seen_this_search
            print(f"  [search page {page}]: {len(links)} links ({len(new_links)} new)")
            if page == 1 and not links:
                _debug_dump_hrefs(html)
            seen_this_search |= links
            if not new_links:
                break
        all_links |= seen_this_search

    print(f"[{SITE_NAME}] {len(all_links)} unique listing URLs found")

    listings: list[Listing] = []
    for url in sorted(all_links):
        html = fetch(url)
        if not html:
            continue
        listing = _parse_detail_page(url, html)
        if listing:
            listings.append(listing)

    print(f"[{SITE_NAME}] parsed {len(listings)} listings")
    return listings
