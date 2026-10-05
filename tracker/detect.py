"""Decide which docket entries point to a large corporate Chapter 11.

The feed carries no dollar figures, so size is inferred from what gets filed.
Big cases hire a claims agent, ask for complex case treatment and file a stack
of first day motions. These signals over-include on purpose; the enrichment
step applies the real size threshold.
"""
import re

PETITION = re.compile(r"voluntary petition.{0,60}chapter\s*11|chapter\s*11.{0,60}voluntary petition", re.I)

SIGNALS = {
    "claims_agent": re.compile(r"claims[,/ ]+(and\s+)?noticing(\s+agent)?|noticing[, ]+(and\s+)?claims", re.I),
    "complex_case": re.compile(r"complex\s+(chapter\s*11|bankruptcy|case)", re.I),
    "first_day": re.compile(r"first[- ]day", re.I),
    "dip": re.compile(r"debtor[- ]in[- ]possession financing|post-?petition financing|\bDIP\s+(financing|facility|motion|order|credit)", re.I),
    "cash_management": re.compile(r"cash management", re.I),
    "critical_vendors": re.compile(r"critical vendor", re.I),
    "joint_admin": re.compile(r"joint(ly)?\s+administ", re.I),
}
STRONG = {"claims_agent", "complex_case"}
# Affiliates in a jointly administered family each get a joint administration
# order, so that signal alone must never promote a case.
WEAK = {"joint_admin"}


def is_ch11_petition(text):
    return bool(PETITION.search(text or ""))


def signals(text):
    return {name for name, rx in SIGNALS.items() if rx.search(text or "")}


def should_promote(found):
    found = set(found)
    if found & STRONG:
        return True
    return len(found - WEAK) >= 2
