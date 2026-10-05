"""Validate and normalize a case entry before it can reach the site."""
import re
from datetime import date

SIDES = ("Debtor", "Creditor", "Committee", "Sponsor", "Other")
ROLES = ("counsel", "bank", "fa")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:60]


def _s(v, n):
    return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""


def _firms(v):
    out = []
    for x in v if isinstance(v, list) else []:
        x = _s(x, 80)
        if x and x not in out:
            out.append(x)
    return out[:8]


def clean_case(d):
    """Return a normalized copy of a case, or raise ValueError saying what is wrong."""
    if not isinstance(d, dict):
        raise ValueError("case is not an object")
    company = _s(d.get("company"), 120)
    if not company:
        raise ValueError("case has no company name")
    filed = _s(d.get("filed"), 10)
    if filed:
        try:
            date.fromisoformat(filed)
        except ValueError:
            raise ValueError(f"{company}: filed date {filed!r} is not YYYY-MM-DD")
    parties = []
    for p in d.get("parties") if isinstance(d.get("parties"), list) else []:
        if not isinstance(p, dict) or not _s(p.get("name"), 120):
            continue
        side = _s(p.get("side"), 20)
        row = {"name": _s(p.get("name"), 120), "note": _s(p.get("note"), 140),
               "side": side if side in SIDES else "Other"}
        for r in ROLES:
            row[r] = _firms(p.get(r))
        parties.append(row)
    if not parties:
        raise ValueError(f"{company}: no parties listed")
    sources = []
    for s in d.get("sources") if isinstance(d.get("sources"), list) else []:
        if not isinstance(s, dict):
            continue
        url = _s(s.get("url"), 500)
        if re.match(r"^https?://[^\s<>\"']+$", url):
            sources.append({"label": _s(s.get("label"), 80) or "Source", "url": url})
    out = {
        "id": slug(d.get("id") or company),
        "company": company,
        "filed": filed,
        "court": _s(d.get("court"), 40),
        "case_no": _s(d.get("case_no"), 20),
        "type": _s(d.get("type"), 40),
        "debt": _s(d.get("debt"), 60),
        "status": _s(d.get("status"), 140),
        "summary": _s(d.get("summary"), 900),
        "lesson": _s(d.get("lesson"), 500),
        "parties": parties[:20],
        "sources": sources[:10],
        "updated": _s(d.get("updated"), 10),
    }
    if d.get("final") is True:
        out["final"] = True
    return out


def blanks(case):
    """Party and role pairs with no firm named, for the reviewer to chase."""
    labels = {"counsel": "counsel", "bank": "investment bank", "fa": "financial advisor"}
    return [f"{p['name']}: {labels[r]}" for p in case["parties"] for r in ROLES if not p[r]]
