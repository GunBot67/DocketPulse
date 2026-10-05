"""Command line entry points. Run `python -m tracker --help`."""
import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from . import detect, enrich, feeds, schema, store

ROOT = store.ROOT
CASES = ROOT / "data" / "cases.json"
STATE = ROOT / "data" / "state.json"
CONFIG = ROOT / "config.json"
DONE = ("drafted", "queued", "skipped", "duplicate", "expired")


def _state():
    s = store.load(STATE, {}) or {}
    s.setdefault("candidates", {})
    s.setdefault("enrich_log", {})
    return s


def _year_ok(case_no, today):
    yy = int(case_no[:2])
    ok = {today.year % 100}
    if today.month == 1:
        ok.add((today.year - 1) % 100)
    return yy in ok


def poll(cfg, state, today=None, fetcher=None, log=print):
    """Read each court feed, track new Chapter 11 cases and promote the big ones."""
    today = today or date.today()
    fetcher = fetcher or (lambda url: feeds.fetch(url, cfg.get("contact", "")))
    cands = state["candidates"]
    stats = {"entries": 0, "new": 0, "promoted": 0, "errors": []}
    for court, url in cfg["courts"].items():
        try:
            items = feeds.parse(fetcher(url), court)
        except Exception as e:  # one dead feed must not stop the others
            stats["errors"].append(f"{court}: {e}")
            continue
        stats["entries"] += len(items)
        for it in items:
            if not _year_ok(it["case_no"], today):
                continue
            found = detect.signals(it["text"])
            petition = detect.is_ch11_petition(it["text"])
            key = f"{court}:{it['case_no']}"
            c = cands.get(key)
            if c is None:
                if not (petition or found):
                    continue
                c = cands[key] = {"court": court, "case_no": it["case_no"], "name": it["name"],
                                  "first_seen": today.isoformat(), "link": it["link"],
                                  "signals": [], "status": "watching"}
                stats["new"] += 1
            c["last_seen"] = today.isoformat()
            if not c.get("name"):
                c["name"] = it["name"]
            c["signals"] = sorted(set(c["signals"]) | found)
            if c["status"] == "watching" and detect.should_promote(c["signals"]):
                c["status"] = "promoted"
                stats["promoted"] += 1
                log(f"promoted {key} {c['name']} {c['signals']}")
    for key, c in list(cands.items()):
        age = (today - date.fromisoformat(c["first_seen"])).days
        if c["status"] == "watching" and age > cfg.get("watch_days", 14):
            c["status"] = "expired"
        if c["status"] in DONE and age > 120:
            del cands[key]
    for e in stats["errors"]:
        log("feed error " + e)
    return stats


def _pr_section(case, heading):
    rows = ["| Party | Counsel | Investment bank | Financial advisor |", "|---|---|---|---|"]
    for p in case["parties"]:
        cell = lambda k: ", ".join(p[k]) or "*not identified*"
        rows.append(f"| {p['name']} | {cell('counsel')} | {cell('bank')} | {cell('fa')} |")
    out = [f"## {heading}: {case['company']}", "",
           f"Filed {case['filed'] or 'date unknown'} in {case['court'] or 'court unknown'}. {case['status']}", "",
           case["summary"], "", *rows, "", "Sources to check:"]
    out += [f"- [{s['label']}]({s['url']})" for s in case["sources"]] or ["- none returned, so verify everything by hand"]
    gaps = schema.blanks(case)
    if gaps:
        out += ["", "Still blank: " + "; ".join(gaps)]
    return "\n".join(out)


PR_HEAD = ("These entries were drafted automatically. Before merging, open the sources and confirm "
           "every firm name and role. Edit `data/cases.json` in this pull request to fix anything. "
           "Merging publishes to the site.\n")


def _finish(cases, sections, pr_body, log):
    if not sections:
        log("nothing to propose")
        return
    store.save(CASES, store.sort_cases(cases))
    if pr_body:
        Path(pr_body).write_text(PR_HEAD + "\n" + "\n\n".join(sections) + "\n", encoding="utf-8")


def run_enrich(cfg, state, cases, today=None, caller=enrich.call_claude, log=print):
    """Draft entries for promoted cases. Returns pull request sections."""
    today = today or date.today()
    used = state["enrich_log"].get(today.isoformat(), 0)
    ids = {c["id"] for c in cases}
    sections = []
    for key, c in state["candidates"].items():
        if c["status"] != "promoted":
            continue
        if len(sections) >= cfg.get("max_enrich_per_run", 3) or used >= cfg.get("max_enrich_per_day", 8):
            break
        used += 1
        hint = _hint(c, cfg)
        try:
            d = enrich.research(hint, cfg, caller=caller)
            if d.get("skip"):
                c["status"], c["reason"] = "skipped", str(d.get("reason", ""))[:200]
                log(f"skipped {key}: {c['reason']}")
                continue
            d.setdefault("case_no", c["case_no"])
            d["updated"] = today.isoformat()
            case = schema.clean_case(d)
        except Exception as e:
            c["attempts"] = c.get("attempts", 0) + 1
            if c["attempts"] >= 3:
                c["status"], c["reason"] = "skipped", f"failed three times: {e}"[:200]
            log(f"error on {key}: {e}")
            continue
        too_old = case["filed"] and (today - date.fromisoformat(case["filed"])).days > cfg.get("max_case_age_days", 45)
        if case["id"] in ids:
            c["status"] = "duplicate"
        elif too_old:
            c["status"], c["reason"] = "skipped", "case predates the tracking window"
        else:
            cases.append(case)
            ids.add(case["id"])
            c["status"], c["case_id"] = "drafted", case["id"]
            sections.append(_pr_section(case, "New case"))
            log(f"drafted {case['company']}")
    state["enrich_log"] = {today.isoformat(): used}
    return sections


def run_refresh(cfg, cases, today=None, caller=enrich.call_claude, log=print):
    """Re-research recent open cases so blanks fill in as committees form and retentions get filed."""
    today = today or date.today()
    sections = []
    for i, old in enumerate(cases):
        if old.get("final") or not old.get("filed"):
            continue
        if (today - date.fromisoformat(old["filed"])).days > cfg.get("refresh_days", 75):
            continue
        try:
            d = enrich.research({}, cfg, existing=old, caller=caller)
            d["id"], d["updated"] = old["id"], today.isoformat()
            d.setdefault("case_no", old.get("case_no", ""))
            new = schema.clean_case(d)
        except Exception as e:
            log(f"error refreshing {old['company']}: {e}")
            continue
        same = {k: v for k, v in new.items() if k != "updated"} == {k: v for k, v in old.items() if k != "updated"}
        if not same:
            cases[i] = new
            sections.append(_pr_section(new, "Update"))
            log(f"updated {new['company']}")
    return sections


ISSUE_INTRO = """A large Chapter 11 was flagged by the court feed. To add it to the site:

1. Copy everything in the box below into a chatbot that can search the web.
2. Read the answer and check the firm names against the sources it gives.
3. Paste the answer as a comment on this issue. The site updates itself and this issue closes.

If the chatbot says the case is too small, or you do not want it on the site, just close this issue.
"""
FENCE = "`" * 3


def _hint(c, cfg):
    return {"court": cfg.get("court_labels", {}).get(c["court"], c["court"]), "case_number": c["case_no"],
            "debtor_name_on_docket": c["name"], "first_seen": c["first_seen"]}


def run_queue(cfg, state, out, log=print):
    """Free mode: write one issue per flagged case, holding a ready-made research prompt."""
    out = Path(out)
    n = 0
    for key, c in state["candidates"].items():
        if c["status"] != "promoted" or n >= cfg.get("max_enrich_per_run", 3):
            continue
        out.mkdir(parents=True, exist_ok=True)
        prompt = enrich.build_prompt(_hint(c, cfg), cfg).replace(FENCE, "")
        title = "New case: " + re.sub(r"[\r\n]+", " ", c["name"] or c["case_no"])[:120]
        body = f"{ISSUE_INTRO}\n{FENCE}\n{prompt}\n{FENCE}\n\n<!-- candidate: {key} -->\n"
        (out / f"{n:02d}.md").write_text(title + "\n" + body, encoding="utf-8")
        c["status"] = "queued"
        n += 1
        log(f"queued {key} {c['name']}")
    return n


def import_reply(text, issue_body, state, cases, today=None):
    """Take a pasted research answer, validate it and add or replace the entry. Returns a message."""
    today = today or date.today()
    m = re.search(r"<!-- candidate: (\S+) -->", issue_body or "")
    cand = state["candidates"].get(m.group(1)) if m else None
    d = enrich.extract_json(text)
    if d.get("skip"):
        if cand:
            cand["status"], cand["reason"] = "skipped", str(d.get("reason", ""))[:200]
        return "Marked as skipped. Nothing was added to the site."
    if cand:
        d.setdefault("case_no", cand["case_no"])
    d["updated"] = today.isoformat()
    case = schema.clean_case(d)
    verb = "Added"
    for i, old in enumerate(cases):
        if old["id"] == case["id"]:
            cases[i], verb = case, "Updated"
            break
    else:
        cases.append(case)
    if cand:
        cand["status"], cand["case_id"] = "drafted", case["id"]
    gaps = schema.blanks(case)
    note = ("\n\nStill blank: " + "; ".join(gaps)) if gaps else ""
    return f"{verb} **{case['company']}** with {len(case['parties'])} parties. The site will update in a few minutes.{note}"


def validate(log=print):
    cases = store.load(CASES, [])
    seen, problems = set(), []
    for c in cases if isinstance(cases, list) else [None]:
        try:
            cid = schema.clean_case(c)["id"]
            if cid in seen:
                problems.append(f"duplicate id {cid}")
            seen.add(cid)
        except ValueError as e:
            problems.append(str(e))
    for p in problems:
        log("invalid: " + p)
    return not problems


def build(out="dist", inline=False):
    """Assemble the static site. With inline=True the data is embedded in a single file."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    cases = [schema.clean_case(c) for c in store.load(CASES, [])]
    if inline:
        blob = json.dumps(cases, ensure_ascii=False).replace("</", "<\\/")
        page = page.replace("/*__CASES__*/null", blob)
    else:
        store.save(out / "data" / "cases.json", cases)
    (out / "index.html").write_text(page, encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracker", description="Large Chapter 11 advisor tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="read court feeds and flag new large cases")
    p.add_argument("--dry-run", action="store_true", help="print what was found without saving")
    for name, text in (("enrich", "draft entries for flagged cases"), ("refresh", "update recent open cases")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--pr-body", help="write a pull request description to this file")
    p = sub.add_parser("add", help="draft an entry for a case you name yourself")
    p.add_argument("company")
    p.add_argument("--pr-body")
    p = sub.add_parser("queue", help="free mode: write a research prompt for each flagged case")
    p.add_argument("--out", required=True)
    p = sub.add_parser("import-comment", help="free mode: add the entry pasted into an issue comment")
    p.add_argument("--event", required=True, help="path to the GitHub event JSON")
    p = sub.add_parser("prompt", help="print a research prompt for a company, to paste into a chatbot")
    p.add_argument("company")
    sub.add_parser("validate", help="check data/cases.json")
    p = sub.add_parser("build", help="assemble the site into dist/")
    p.add_argument("--out", default="dist")
    p.add_argument("--inline", action="store_true")
    a = ap.parse_args(argv)
    cfg = store.load(CONFIG)
    if a.cmd in ("enrich", "refresh", "add") and not os.environ.get("ANTHROPIC_API_KEY"):
        # Exit cleanly so flagged cases wait for the key instead of being marked as failures.
        print("ANTHROPIC_API_KEY is not set, so no research was done. Flagged cases stay queued.")
        return

    if a.cmd == "poll":
        state = _state()
        stats = poll(cfg, state)
        print(json.dumps(stats, indent=2))
        if a.dry_run:
            print(json.dumps(state["candidates"], indent=2))
        else:
            store.save(STATE, state)
    elif a.cmd == "enrich":
        state, cases = _state(), store.load(CASES, [])
        sections = run_enrich(cfg, state, cases)
        store.save(STATE, state)
        _finish(cases, sections, a.pr_body, print)
    elif a.cmd == "refresh":
        cases = store.load(CASES, [])
        _finish(cases, run_refresh(cfg, cases), a.pr_body, print)
    elif a.cmd == "add":
        cases = store.load(CASES, [])
        d = enrich.research({"company": a.company, "note": "added by hand, so any filing date is in scope"}, cfg)
        if d.get("skip"):
            sys.exit("not added: " + str(d.get("reason", "")))
        d["updated"] = date.today().isoformat()
        case = schema.clean_case(d)
        if case["id"] in {c["id"] for c in cases}:
            sys.exit(f"{case['company']} is already in data/cases.json")
        cases.append(case)
        _finish(cases, [_pr_section(case, "New case")], a.pr_body, print)
        print(f"drafted {case['company']}; review data/cases.json before publishing")
    elif a.cmd == "queue":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return  # the enrich step handles flagged cases when a key is present
        state = _state()
        run_queue(cfg, state, a.out)
        store.save(STATE, state)
    elif a.cmd == "import-comment":
        ev = json.loads(Path(a.event).read_text(encoding="utf-8"))
        state, cases = _state(), store.load(CASES, [])
        try:
            msg = import_reply(ev["comment"]["body"], ev["issue"].get("body") or "", state, cases)
        except Exception as e:
            print(f"That could not be added: {e}\n\nFix the pasted answer and comment again.")
            sys.exit(1)
        store.save(CASES, store.sort_cases(cases))
        store.save(STATE, state)
        print(msg)
    elif a.cmd == "prompt":
        print(enrich.build_prompt({"company": a.company, "note": "any filing date is in scope"}, cfg))
    elif a.cmd == "validate":
        sys.exit(0 if validate() else 1)
    elif a.cmd == "build":
        build(a.out, a.inline)
        print(f"built {a.out}/")
