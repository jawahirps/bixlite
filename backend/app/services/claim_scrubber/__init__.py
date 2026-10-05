"""
claim_scrubber — UAE eClaimLink XML pre-submission scrubber.

Primary entry point: scrub_xml_bytes(xml_bytes, profile) → dict
"""
from .scrubber import scrub, load_rule_pack, load_nas_guidelines, load_calibration, PROFILE_SOURCES
import tempfile
from pathlib import Path


def scrub_xml_bytes(
    xml_bytes: bytes,
    profile: str = "UAE_COMMON",
    nas_guidelines_path: Path | None = None,
    calibration_path: Path | None = None,
) -> dict:
    """
    Run the claim scrubber on raw XML bytes.

    Returns the full report dict with keys:
        status, summary, findings, profile, rule_pack, guidelines, calibration
    """
    rule_pack = load_rule_pack()
    nas = load_nas_guidelines(nas_guidelines_path) if nas_guidelines_path else None
    cal = load_calibration(calibration_path) if calibration_path else None

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
        fh.write(xml_bytes)
        tmp = Path(fh.name)
    try:
        return scrub(tmp, profile=profile, rule_pack=rule_pack, nas_guidelines=nas, calibration=cal)
    finally:
        tmp.unlink(missing_ok=True)
