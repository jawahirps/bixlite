"""eClaimLink payer / TPA / self-paid names, from the DHA eClaimLink payer lists
(Payers list 29 Apr 2026 + Self-paid list 12 May 2026) in app/data/eclaimlink_payers.json."""
import json
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).resolve().parent.parent / "data" / "eclaimlink_payers.json"


@lru_cache(maxsize=1)
def _directory() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def payer_name(code, fallback=None):
    """Return the eClaimLink name for a payer code, else the fallback (if it isn't just the code)."""
    if code:
        hit = _directory().get(str(code).strip().upper())
        if hit:
            return hit["name"]
    if fallback and fallback != code:
        return fallback
    return None
