"""Fetch and parse the free CM/ECF RSS feeds that bankruptcy courts publish.

Each feed lists docket entries from roughly the last 12 to 24 hours, so it has
to be polled at least a few times a day.
"""
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

CASE_RE = re.compile(r"\b(\d{2})-(?:bk-)?(\d{5})\b", re.I)
TAG_RE = re.compile(r"<[^>]+>")


class FeedError(Exception):
    pass


def fetch(url, contact="", timeout=30):
    ua = "rx-tracker/1.0" + (f" ({contact})" if contact else "")
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8_000_000)


def _text(node, tag):
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse(xml_bytes, court):
    """Return a list of docket entries: court, case_no, name, text, link, published."""
    head = xml_bytes[:4000].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise FeedError("feed declares a DOCTYPE or entities; refusing to parse")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FeedError(f"not valid XML: {e}") from e
    items = []
    for node in root.iter("item"):
        title = html.unescape(_text(node, "title"))
        m = CASE_RE.search(title)
        if not m:
            continue
        name = re.sub(r"^-[A-Za-z]{2,4}\b", "", title[m.end():]).strip(" -:\t")
        text = html.unescape(TAG_RE.sub(" ", html.unescape(_text(node, "description"))))
        published = ""
        try:
            published = parsedate_to_datetime(_text(node, "pubDate")).date().isoformat()
        except (TypeError, ValueError):
            pass
        items.append({
            "court": court,
            "case_no": f"{m.group(1)}-{m.group(2)}",
            "name": name[:200],
            "text": re.sub(r"\s+", " ", text).strip()[:500],
            "link": _text(node, "link")[:500],
            "published": published,
        })
    return items
