from __future__ import annotations

import re
from dataclasses import dataclass


TWAP_TOPIC_BY_WINDOW = {
    30: "crypto_prices_twap_thirty",
    60: "crypto_prices_twap_sixty",
}
SUPPORTED_TWAP_WINDOWS = tuple(sorted(TWAP_TOPIC_BY_WINDOW))


@dataclass(frozen=True, slots=True)
class ResolutionTwapContract:
    source: str | None
    status: str
    window_seconds: int | None
    topic: str | None

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"


def resolution_twap_contract(source: str | None) -> ResolutionTwapContract:
    """Extract an explicit supported TWAP window from the resolution source.

    The market's own resolution source is authoritative. Generic mentions of
    Chainlink or TWAP are intentionally not enough to infer a window.
    """
    if source is None or not source.strip():
        return ResolutionTwapContract(source, "MISSING", None, None)

    normalized = source.strip().lower()
    detected: set[int] = set()
    patterns = {
        30: (
            r"twap[-_ ]?30s(?:[-_/ ]|$)",
            r"twap\s*:\s*30\s*(?:s|sec|seconds?)\b",
            r"crypto_prices_twap_thirty\b",
        ),
        60: (
            r"twap[-_ ]?60s(?:[-_/ ]|$)",
            r"twap\s*:\s*60\s*(?:s|sec|seconds?)\b",
            r"crypto_prices_twap_sixty\b",
        ),
    }
    for window_seconds, candidates in patterns.items():
        if any(re.search(pattern, normalized) for pattern in candidates):
            detected.add(window_seconds)

    if len(detected) > 1:
        return ResolutionTwapContract(source, "AMBIGUOUS", None, None)
    if not detected:
        return ResolutionTwapContract(source, "UNSUPPORTED", None, None)
    window_seconds = detected.pop()
    return ResolutionTwapContract(
        source,
        "VERIFIED",
        window_seconds,
        TWAP_TOPIC_BY_WINDOW[window_seconds],
    )


def twap_topic_for_window(window_seconds: int) -> str:
    try:
        return TWAP_TOPIC_BY_WINDOW[int(window_seconds)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Ventana TWAP no compatible: {window_seconds!r}"
        ) from exc
