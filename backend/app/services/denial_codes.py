"""DHA eClaimLink denial code descriptions (Denial Codes list, 29 Oct 2024) in app/data/denial_codes.json."""
import json
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).resolve().parent.parent / "data" / "denial_codes.json"


@lru_cache(maxsize=1)
def _codes() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def denial_description(code, fallback=None):
    """Official eClaimLink description for a denial code, else the description the payer sent on the RA."""
    hit = _codes().get(str(code or "").strip().upper())
    return hit["description"] if hit else (fallback or None)
