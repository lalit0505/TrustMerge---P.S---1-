"""Evidence levels for testing provenance.

Ordered from strongest to weakest.  UNKNOWN means the tool found no
evidence at all — it never defaults to "safe".
"""

PROD = "PROD"
UAT = "UAT"
CI_PASSED = "CI-PASSED"
UNVERIFIED = "UNVERIFIED"
UNKNOWN = "UNKNOWN"

LEVEL_ORDER = [PROD, UAT, CI_PASSED, UNVERIFIED, UNKNOWN]
RECORDABLE = {PROD, UAT, CI_PASSED}


def rank(level: str) -> int:
    """Return the ordinal position of *level* (lower = stronger).

    Unknown levels return a high number (ignored / weakest) without raising.
    """
    if not isinstance(level, str):
        return 999
    norm = level.upper().replace("_", "-")
    try:
        return LEVEL_ORDER.index(norm)
    except ValueError:
        return 999


def stronger(a: str, b: str) -> str:
    """Return whichever of *a* and *b* represents stronger evidence."""
    return a if rank(a) <= rank(b) else b

