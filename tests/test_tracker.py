import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tracker import cli, detect, enrich, feeds, schema

TODAY = date(2026, 10, 5)


def feed(*items):
    body = "".join(
        f"<item><title>{t}</title><pubDate>Mon, 05 Oct 2026 14:03:11 GMT</pubDate>"
        f"<link>https://ecf.example/cgi-bin/DktRpt.pl?{i}</link>"
        f"<description><![CDATA[[{d}] (<a href=\"https://ecf.example/doc1/{i}\">{i}</a>)]]></description></item>"
        for i, (t, d) in enumerate(items, 1))
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{body}</channel></rss>".encode()


BIG = feed(
    ("26-19876-MBK Acme Holdings, Inc.", "Voluntary Petition (Chapter 11)"),
    ("26-19876-MBK Acme Holdings, Inc.", "Application to Appoint Claims and Noticing Agent"),
    ("26-19877-MBK Acme Sub LLC", "Voluntary Petition (Chapter 11)"),
    ("26-19877-MBK Acme Sub LLC", "Order Directing Joint Administration"),
    ("26-19900 Corner Deli LLC", "Voluntary Petition (Chapter 11)"),
    ("26-18000 Jane Doe", "Voluntary Petition (Chapter 13)"),
    ("19-10001 Old Case Corp", "Motion to Approve DIP Financing and Cash Management"),
)
CFG = {"courts": {"njb": "u"}, "court_labels": {"njb": "D.N.J."}, "watch_days": 14,
       "max_enrich_per_run": 3, "max_enrich_per_day": 8, "max_case_age_days": 45, "refresh_days": 75}
DRAFT = {"company": "Acme", "filed": "2026-10-05", "court": "D.N.J.", "type": "Prearranged",
         "summary": "S.", "parties": [{"name": "Acme", "side": "Debtor", "counsel": ["Kirkland & Ellis"],
                                       "bank": ["Evercore"], "fa": []}],
         "sources": [{"label": "PR", "url": "https://example.com/pr"}, {"label": "bad", "url": "javascript:alert(1)"}]}


def new_state():
    return {"candidates": {}, "enrich_log": {}}


class Feeds(unittest.TestCase):
    def test_parse(self):
        items = feeds.parse(BIG, "njb")
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0]["case_no"], "26-19876")
        self.assertEqual(items[0]["name"], "Acme Holdings, Inc.")
        self.assertIn("Voluntary Petition (Chapter 11)", items[0]["text"])
        self.assertNotIn("<a", items[0]["text"])
        self.assertEqual(items[0]["published"], "2026-10-05")

    def test_district_style_number(self):
        items = feeds.parse(feed(("1:26-bk-12345 Foo Corp", "Voluntary Petition Chapter 11")), "x")
        self.assertEqual((items[0]["case_no"], items[0]["name"]), ("26-12345", "Foo Corp"))

    def test_rejects_entities_and_garbage(self):
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><rss/>", "x")
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"<html>Access denied", "x")


class Detect(unittest.TestCase):
    def test_petition(self):
        self.assertTrue(detect.is_ch11_petition("Chapter 11 Voluntary Petition Non-Individual"))
        self.assertFalse(detect.is_ch11_petition("Voluntary Petition (Chapter 7)"))
        self.assertFalse(detect.is_ch11_petition("Chapter 11 Plan of Reorganization"))

    def test_promotion(self):
        self.assertTrue(detect.should_promote(detect.signals("Motion to retain Kroll as Claims, Noticing and Solicitation Agent")))
        self.assertTrue(detect.should_promote(detect.signals("Notice of Designation as Complex Chapter 11 Case")))
        self.assertTrue(detect.should_promote({"first_day", "dip"}))
        self.assertFalse(detect.should_promote({"joint_admin"}))
        self.assertFalse(detect.should_promote({"joint_admin", "cash_management"}))
        self.assertFalse(detect.should_promote(set()))


class Poll(unittest.TestCase):
    def test_only_the_lead_case_is_promoted(self):
        st = new_state()
        stats = cli.poll(CFG, st, TODAY, fetcher=lambda u: BIG, log=lambda *_: None)
        c = st["candidates"]
        self.assertEqual(c["njb:26-19876"]["status"], "promoted")
        self.assertEqual(c["njb:26-19877"]["status"], "watching")
        self.assertEqual(c["njb:26-19900"]["status"], "watching")
        self.assertNotIn("njb:26-18000", c)
        self.assertNotIn("njb:19-10001", c)
        self.assertEqual(stats["promoted"], 1)
        cli.poll(CFG, st, TODAY, fetcher=lambda u: BIG, log=lambda *_: None)
        self.assertEqual(len(c), 3)

    def test_dead_feed_and_expiry(self):
        st = new_state()
        cli.poll(CFG, st, TODAY, fetcher=lambda u: BIG, log=lambda *_: None)

        def boom(u):
            raise OSError("timeout")
        stats = cli.poll(CFG, st, date(2026, 10, 25), fetcher=boom, log=lambda *_: None)
        self.assertEqual(len(stats["errors"]), 1)
        self.assertEqual(st["candidates"]["njb:26-19900"]["status"], "expired")
        self.assertEqual(st["candidates"]["njb:26-19876"]["status"], "promoted")


class Enrich(unittest.TestCase):
    def setUp(self):
        self.st = new_state()
        cli.poll(CFG, self.st, TODAY, fetcher=lambda u: BIG, log=lambda *_: None)

    def run_with(self, reply, cases=None):
        cases = [] if cases is None else cases
        secs = cli.run_enrich(CFG, self.st, cases, TODAY, caller=lambda p, c: reply, log=lambda *_: None)
        return cases, secs, self.st["candidates"]["njb:26-19876"]

    def test_draft(self):
        cases, secs, cand = self.run_with("Here you go:\n```json\n" + json.dumps(DRAFT) + "\n```")
        self.assertEqual(cand["status"], "drafted")
        self.assertEqual(cases[0]["id"], "acme")
        self.assertEqual(cases[0]["case_no"], "26-19876")
        self.assertEqual(len(cases[0]["sources"]), 1)
        self.assertIn("Kirkland & Ellis", secs[0])
        self.assertIn("Acme: financial advisor", secs[0])
        self.assertEqual(self.run_with("should not be called")[1], [])

    def test_skip_duplicate_old_and_errors(self):
        self.assertEqual(self.run_with('{"skip": true, "reason": "under the size floor"}')[2]["status"], "skipped")
        self.setUp()
        self.assertEqual(self.run_with(json.dumps(DRAFT), [schema.clean_case(DRAFT)])[2]["status"], "duplicate")
        self.setUp()
        self.assertEqual(self.run_with(json.dumps({**DRAFT, "filed": "2026-03-01"}))[2]["status"], "skipped")
        self.setUp()
        for _ in range(2):
            self.assertEqual(self.run_with("no json here")[2]["status"], "promoted")
        self.assertEqual(self.run_with("no json here")[2]["status"], "skipped")

    def test_daily_cap(self):
        self.st["enrich_log"] = {TODAY.isoformat(): 8}
        self.assertEqual(self.run_with(json.dumps(DRAFT))[2]["status"], "promoted")

    def test_refresh(self):
        old = schema.clean_case(DRAFT)
        newer = {**DRAFT, "parties": DRAFT["parties"] + [{"name": "UCC", "side": "Committee", "counsel": ["MoFo"]}]}
        cases = [old]
        secs = cli.run_refresh(CFG, cases, TODAY, caller=lambda p, c: json.dumps(newer), log=lambda *_: None)
        self.assertEqual(len(secs), 1)
        self.assertEqual(len(cases[0]["parties"]), 2)
        self.assertEqual(cli.run_refresh(CFG, cases, TODAY, caller=lambda p, c: json.dumps(newer), log=lambda *_: None), [])
        final = [{**old, "final": True}]
        self.assertEqual(cli.run_refresh(CFG, final, TODAY, caller=lambda p, c: 1 / 0, log=lambda *_: None), [])

    def test_prompt_mentions_floor_and_existing(self):
        p = enrich.build_prompt({"debtor_name_on_docket": "X"}, {"min_liabilities_musd": 500})
        self.assertIn("$500 million", p)
        self.assertIn("Current entry", enrich.build_prompt({}, {}, existing={"company": "X"}))


class FreeMode(unittest.TestCase):
    def setUp(self):
        self.st = new_state()
        cli.poll(CFG, self.st, TODAY, fetcher=lambda u: BIG, log=lambda *_: None)

    def test_queue_then_import(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cli.run_queue(CFG, self.st, d, log=lambda *_: None), 1)
            text = (Path(d) / "00.md").read_text()
            self.assertEqual(cli.run_queue(CFG, self.st, d, log=lambda *_: None), 0)
        self.assertTrue(text.startswith("New case: Acme Holdings, Inc.\n"))
        self.assertIn("<!-- candidate: njb:26-19876 -->", text)
        self.assertEqual(text.count("`" * 3), 2)
        cases = []
        msg = cli.import_reply("Sure!\n" + json.dumps(DRAFT), text, self.st, cases, TODAY)
        self.assertIn("Added **Acme**", msg)
        self.assertEqual(cases[0]["case_no"], "26-19876")
        self.assertEqual(self.st["candidates"]["njb:26-19876"]["status"], "drafted")
        msg = cli.import_reply(json.dumps({**DRAFT, "status": "Emerged."}), "", self.st, cases, TODAY)
        self.assertIn("Updated", msg)
        self.assertEqual((len(cases), cases[0]["status"]), (1, "Emerged."))

    def test_import_skip_and_garbage(self):
        cases = []
        cli.import_reply('{"skip": true, "reason": "small"}', "<!-- candidate: njb:26-19876 -->", self.st, cases, TODAY)
        self.assertEqual((cases, self.st["candidates"]["njb:26-19876"]["status"]), ([], "skipped"))
        for bad in ("thanks {", '{"company": "X", "parties": []}'):
            with self.assertRaises(ValueError):
                cli.import_reply(bad, "", self.st, cases, TODAY)


class Schema(unittest.TestCase):
    def test_rejects_bad_entries(self):
        for bad in ({}, {"company": "X"}, {"company": "X", "filed": "Oct 5", "parties": [{"name": "X"}]}, []):
            with self.assertRaises(ValueError):
                schema.clean_case(bad)

    def test_seed_data_and_build(self):
        self.assertTrue(cli.validate(log=lambda *_: None))
        with tempfile.TemporaryDirectory() as d:
            cli.build(d)
            self.assertGreaterEqual(len(json.loads((Path(d) / "data" / "cases.json").read_text())), 3)
            self.assertIn("/*__CASES__*/null", (Path(d) / "index.html").read_text())
            cli.build(d, inline=True)
            page = (Path(d) / "index.html").read_text()
            self.assertNotIn("/*__CASES__*/null", page)
            self.assertIn("Brightline", page)


if __name__ == "__main__":
    unittest.main()
