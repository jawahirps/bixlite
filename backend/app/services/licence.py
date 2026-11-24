"""Clinician licence normalisation.

The same doctor can appear under several licence spellings: Riayati / SHA claims prefix the DHA licence with
'SHA' (SHAHPL004646), while eClaimLink uses the bare licence (HPL004646). Compare and
key clinician ids through norm_licence() so these match.
"""
import re

# Only SHA is confirmed by the Ghaf team as the same doctor; MOH-prefixed ids (e.g. MOHD…) are NOT merged.
_PREFIX = re.compile(r"^SHA(?=[A-Z]+\d)")


def norm_licence(lic) -> str:
    """Upper-case, drop spaces, strip a leading SHA prefix (SHAHPL004646 -> HPL004646)."""
    s = re.sub(r"\s+", "", str(lic or "")).upper()
    return _PREFIX.sub("", s)
