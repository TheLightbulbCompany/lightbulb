"""Phone numbers as the assistant dials them: E.164 from what a person types, and only US or Canadian numbers out."""

import re


_E164 = re.compile(r"\+[1-9]\d{6,14}")


def _e164(raw: str) -> str | None:
    """The handoff number as the worker dials it (tel:+…), from what the page's field takes ("(415) 555-0147").
    ponytail: a number without + is read as US (+1); other countries type the +."""
    digits = re.sub(r"\D", "", raw)
    if not raw.strip().startswith("+"):
        digits = "1" + digits if len(digits) == 10 else digits if len(digits) == 11 and digits[0] == "1" else ""
    number = f"+{digits}"
    return number if _E164.fullmatch(number) else None


#: NANP area codes that are not the US or Canada (Caribbean and Atlantic countries, billed as international), and the
#: premium ones. The assistant only ever dials out (hand-off, run-out forward, forwarding check) to a +1 US/CA number.
NON_US_CA_AREA_CODES = frozenset(
    "242 246 264 268 284 345 441 473 649 658 664 721 758 767 784 809 829 849 868 869 876".split()
)


PREMIUM_AREA_CODES = frozenset({"900", "976"})


_NANP = re.compile(r"\+1([2-9]\d{2})[2-9]\d{6}")


def us_number(raw: str) -> str | None:
    """`_e164`, kept only for a +1 US or Canadian number (not premium, not another NANP country): the one check for
    every number we dial out to."""
    number = _e164(raw)
    m = _NANP.fullmatch(number or "")
    if not m or m.group(1) in NON_US_CA_AREA_CODES | PREMIUM_AREA_CODES:
        return None
    return number
