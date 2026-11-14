"""Read a product page she pasted: name, price, description, rating.

Shop pages often block servers, so a failure here is normal and handled by
the caller (Tavily extract, or asking for the product name).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 2_500_000
UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Mobile Safari/537.36")


class FetchError(Exception):
    pass


def check_public_url(url: str) -> str:
    """Only allow normal web addresses, never the server's own network."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("That does not look like a web link. It should start with https://")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise FetchError("That website address could not be found.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchError("That link points to a private address, which is not allowed.")
    return parsed.geturl()


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.ld: list[str] = []
        self.text: list[str] = []
        self._in = ""
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "title":
            self._in = "title"
        elif tag == "script":
            if (a.get("type") or "").lower() == "application/ld+json":
                self._in = "ld"
                self.ld.append("")
            else:
                self._skip += 1
        elif tag in ("style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("title",) or (tag == "script" and self._in == "ld"):
            self._in = ""
        elif tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "ld":
            self.ld[-1] += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def _find_product(node):
    """Find a schema.org Product inside JSON-LD (which can be nested any way)."""
    if isinstance(node, list):
        for item in node:
            hit = _find_product(item)
            if hit:
                return hit
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Product" in kinds:
            return node
        for value in node.values():
            if isinstance(value, (dict, list)):
                hit = _find_product(value)
                if hit:
                    return hit
    return None


def parse_product_html(html: str, url: str = "") -> dict:
    parser = _Meta()
    try:
        parser.feed(html)
    except Exception:  # broken markup: keep whatever was read so far
        pass
    meta = parser.meta
    out = {
        "name": unescape(meta.get("og:title") or parser.title or "").strip(),
        "description": unescape(meta.get("og:description") or meta.get("description") or "").strip(),
        "price": meta.get("product:price:amount") or meta.get("og:price:amount") or "",
        "currency": meta.get("product:price:currency") or meta.get("og:price:currency") or "",
        "rating": "", "review_count": "", "brand": "",
        "text": " ".join(parser.text)[:6000],
        "url": url,
    }
    for raw in parser.ld:
        try:
            product = _find_product(json.loads(raw))
        except (json.JSONDecodeError, RecursionError):
            continue
        if not product:
            continue
        out["name"] = str(product.get("name") or out["name"]).strip()
        out["description"] = str(product.get("description") or out["description"]).strip()
        brand = product.get("brand")
        out["brand"] = str(brand.get("name") if isinstance(brand, dict) else brand or "").strip()
        offers = product.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offer, dict):
            out["price"] = str(offer.get("price") or offer.get("lowPrice") or out["price"])
            out["currency"] = str(offer.get("priceCurrency") or out["currency"])
        rating = product.get("aggregateRating")
        if isinstance(rating, dict):
            out["rating"] = str(rating.get("ratingValue") or "")
            out["review_count"] = str(rating.get("reviewCount") or rating.get("ratingCount") or "")
        break
    out["name"] = re.sub(r"\s+", " ", out["name"])[:200]
    out["description"] = re.sub(r"\s+", " ", out["description"])[:1500]
    return out


def fetch_product_page(url: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    current = url
    with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), transport=transport, follow_redirects=False,
                      headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}) as client:
        for _ in range(5):
            if transport is None:
                current = check_public_url(current)
            try:
                with client.stream("GET", current) as res:
                    if res.status_code in (301, 302, 303, 307, 308) and res.headers.get("location"):
                        current = urljoin(current, res.headers["location"])
                        continue
                    if res.status_code != 200:
                        raise FetchError(f"The shop page answered with error {res.status_code}.")
                    chunks, size = [], 0
                    for chunk in res.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_BYTES:
                            break
                    html = b"".join(chunks).decode(res.encoding or "utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise FetchError(f"The shop page could not be opened ({exc.__class__.__name__}).") from exc
            info = parse_product_html(html, current)
            if not info["name"] and len(info["text"]) < 200:
                raise FetchError("The shop page did not show any product details to the server.")
            return info
    raise FetchError("The link redirected too many times.")
