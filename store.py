import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sort_cases(cases):
    return sorted(cases, key=lambda c: (c.get("filed") or "", c.get("company") or ""), reverse=True)
