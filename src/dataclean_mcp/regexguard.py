"""Refuse regexes whose shape allows catastrophic backtracking (ReDoS)."""

from __future__ import annotations

import re
from typing import Any


class RegexError(ValueError):
    pass


try:  # Python 3.11+
    from re import _constants as _sre_c, _parser as _sre_p  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Python 3.10
    import sre_constants as _sre_c  # type: ignore[no-redef]
    import sre_parse as _sre_p  # type: ignore[no-redef]

MAX_REGEX_LEN = 300
_REPEATS = (_sre_c.MAX_REPEAT, _sre_c.MIN_REPEAT, getattr(_sre_c, "POSSESSIVE_REPEAT", None))


def _risky(items: Any, in_repeat: bool = False) -> bool:
    """True for backreferences or a repeat nested in another repeat: the shapes behind catastrophic
    backtracking, e.g. (a+)+ or (a*)*b, which would hold the server's CPU for minutes."""
    for op, av in items:
        if op in (_sre_c.GROUPREF, getattr(_sre_c, "GROUPREF_EXISTS", None)):
            return True
        if op in _REPEATS and op is not None:
            lo, hi, sub = av
            many = hi is _sre_c.MAXREPEAT or hi > 1
            if many and in_repeat:
                return True
            if _risky(sub, in_repeat or many):
                return True
        elif op is _sre_c.SUBPATTERN:
            if _risky(av[-1], in_repeat):
                return True
        elif op is _sre_c.BRANCH:
            if any(_risky(b, in_repeat) for b in av[1]):
                return True
        elif op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
            if _risky(av[1], in_repeat):
                return True
    return False


def safe_regex(rx: str, where: str) -> re.Pattern[str]:
    if len(rx) > MAX_REGEX_LEN:
        raise RegexError(f"{where}: regex too long (max {MAX_REGEX_LEN} characters)")
    try:
        pattern = re.compile(rx)
    except re.error as exc:
        raise RegexError(f"{where}: invalid regex ({exc})") from exc
    if _risky(_sre_p.parse(rx)):
        raise RegexError(f"{where}: regex with nested quantifiers or backreferences (e.g. (a+)+) "
                           "is refused: it could hang the server")
    return pattern
