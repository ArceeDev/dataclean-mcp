"""Deterministic value parsers: text hygiene, numbers (EU/US), dates (DMY/MDY/YMD), booleans, emails, phones, IBAN.

Every parser returns None when the value does not fit, never a guess. Ambiguous inputs ("1,250" or "03/04/2026")
are reported as ambiguous so the column-level logic can decide from the other values.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

NULLS = frozenset({"", "null", "none", "nil", "nan", "n/a", "na", "#n/a", "#null!", "-", "--", "—", "–", "?",
                   "(blank)", "(empty)", "undefined", "missing"})

# ---------------------------------------------------------------- text hygiene

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t  -   　]+")
_MOJIBAKE = re.compile(r"[ÃÂâ][\x80-\xbfŒœŠšŸŽžƒˆ˜–—"
                       r"‘-„†-•…‰‹›€™]")


def fix_mojibake(s: str) -> str:
    """'CafÃ©' -> 'Café' (UTF-8 bytes that were decoded as Windows-1252). Left alone unless it round-trips."""
    if not _MOJIBAKE.search(s):
        return s
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if len(_MOJIBAKE.findall(fixed)) < len(_MOJIBAKE.findall(s)):
            return fixed
    return s


def clean_text(s: str, collapse: bool = True, mojibake: bool = True) -> str:
    """No NUL/control or zero-width characters, NBSP and odd spaces to plain spaces, mojibake repaired, NFC, trimmed."""
    s = _CONTROL.sub("", s.translate(_ZERO_WIDTH)).replace("\r\n", "\n").replace("\r", "\n")
    s = _SPACES.sub(" ", s) if collapse else s.replace("\u00a0", " ")
    if mojibake and _MOJIBAKE.search(s):
        s = " ".join(fix_mojibake(w) for w in s.split(" "))
    return unicodedata.normalize("NFC", s).strip()


def is_null(s: str, extra: frozenset[str] = frozenset()) -> bool:
    t = s.strip().lower()
    return t in NULLS or t in extra


# ---------------------------------------------------------------- numbers

_SYMBOLS = ("US$", "R$", "A$", "C$", "NZ$", "CHF", "€", "$", "£", "¥", "₹", "₽", "₩", "₺", "₫", "₪", "zł", "Kč", "Ft",
            "kr")
_ISO = frozenset("EUR USD GBP JPY CHF CAD AUD NZD MXN BRL ARS COP CLP PEN SEK NOK DKK PLN CZK HUF RON INR CNY ZAR "
                 "AED ILS SGD HKD TRY".split())
_PLAIN = re.compile(r"^\d+$")
_EU_FULL = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$|^\d+,\d+$")
_US_FULL = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$|^\d+\.\d+$")
_SPACE_GROUPS = re.compile(r"^\d{1,3}([ '\u202f]\d{3})+([.,]\d+)?$")
_SCI = re.compile(r"^\d+(\.\d+)?[eE][+-]?\d+$")
_AMBIG = re.compile(r"^\d{1,3}[.,]\d{3}$")


class Num:
    """A parsed number. locale: 'eu' (decimal comma), 'us' (decimal point), 'plain' (no separators) or
    'ambiguous' ('1,250' / '1.250': thousands or three decimals; resolved by the column)."""

    __slots__ = ("value", "locale", "percent", "currency", "sep")

    def __init__(self, value: Decimal, locale: str, percent: bool = False, currency: str = "", sep: str = "") -> None:
        self.value, self.locale, self.percent, self.currency, self.sep = value, locale, percent, currency, sep


def _dec(s: str) -> Decimal | None:
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _strip_currency(s: str) -> tuple[str, str]:
    cur = ""
    head, tail = s[:3], s[-3:]
    if head in _ISO and s[3:].strip()[:1] in "0123456789+-(.,":
        cur, s = head, s[3:].strip()
    elif tail in _ISO and s[:-3].strip()[-1:] in "0123456789.,)-":
        cur, s = tail, s[:-3].strip()
    for sym in _SYMBOLS:
        if s.startswith(sym):
            return s[len(sym):].strip(), cur or sym
        if s.endswith(sym):
            return s[:-len(sym)].strip(), cur or sym
    for sym in _SYMBOLS:  # '-€12' / '€-12'
        if s[:1] in "+-" and s[1:].startswith(sym):
            return s[0] + s[1 + len(sym):].strip(), cur or sym
    return s, cur


def parse_number(raw: str) -> Num | None:
    s = raw.strip()
    if not s or len(s) > 60:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    s, cur = _strip_currency(s)
    if s.startswith(("+", "-")):
        neg ^= s[0] == "-"
        s = s[1:].strip()
    if s.startswith("(") and s.endswith(")"):
        neg, s = not neg, s[1:-1].strip()
    if s.endswith("-") and s[:-1].strip():  # SAP style trailing minus: 1.250,50-
        neg, s = not neg, s[:-1].strip()
    if s[:1] in (".", ",") and s[1:].isdigit():
        s = "0" + s
    if not s[:1].isdigit():
        return None
    sep = ""
    if _PLAIN.match(s) or _SCI.match(s):
        val, loc = _dec(s), "plain"
    elif _AMBIG.match(s):
        sep = s[-4]
        val, loc = _dec(s.replace(sep, "")), "ambiguous"
    elif _SPACE_GROUPS.match(s):
        body = re.sub(r"[ '\u202f]", "", s)
        loc = "eu" if "," in body else "us" if "." in body else "plain"
        val = _dec(body.replace(",", "."))
    elif _EU_FULL.match(s):
        val, loc = _dec(s.replace(".", "").replace(",", ".")), "eu"
    elif _US_FULL.match(s):
        val, loc = _dec(s.replace(",", "")), "us"
    else:
        return None
    if val is None:
        return None
    return Num(-val if neg else val, loc, pct, cur, sep)


def number_for(n: Num, locale: str) -> Decimal | None:
    """Value under the column locale ('eu' or 'us'); None when an ambiguous value cannot be resolved."""
    if n.locale != "ambiguous":
        return n.value
    if locale not in ("eu", "us"):
        return None
    decimal_sep = "," if locale == "eu" else "."
    return n.value / 1000 if n.sep == decimal_sep else n.value


def canonical(d: Decimal) -> str:
    """Plain decimal keeping the written scale: 1250.50 stays 1250.50, 1E+3 becomes 1000."""
    s = format(d, "f")
    return s.lstrip("-") if d == 0 else s


# ---------------------------------------------------------------- dates

MONTHS = {
    **{m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)},
    **{m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                   "september", "october", "november", "december"], 1)},
    **{m: i for i, m in enumerate(["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"], 1)},
    **{m: i for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                                   "septiembre", "octubre", "noviembre", "diciembre"], 1)},
    "sept": 9, "setiembre": 9, "set": 9,
    **{m: i for i, m in enumerate(["janv", "févr", "mars", "avr", "mai", "juin", "juil", "août", "sept", "oct", "nov",
                                   "déc"], 1)},
    **{m: i for i, m in enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                                   "septembre", "octobre", "novembre", "décembre"], 1)},
    **{m: i for i, m in enumerate(["jän", "feb", "mär", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "dez"], 1)},
    **{m: i for i, m in enumerate(["januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
                                   "september", "oktober", "november", "dezember"], 1)},
}

_TIME = r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?\s*([ap]\.?m\.?)?\s*(Z|[+-]\d{2}:?\d{2})?)?"
_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})" + _TIME + "$", re.I)
_XYZ = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})" + _TIME + "$", re.I)
_TEXT_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th|º)?[\s\-/.]+(?:de\s+)?([a-zéèäûüç]{3,10})\.?[\s\-/.,]+(?:de\s+)?"
                       r"(\d{2}|\d{4})" + _TIME + "$", re.I)
_TEXT_MDY = re.compile(r"^([a-zéèäûüç]{3,10})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})" + _TIME + "$", re.I)
_WEEKDAY = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun|lun|mar|mié|mie|jue|vie|sáb|sab|dom)[a-zéá]*\.?,\s*", re.I)
_COMPACT = re.compile(r"^(19|20)(\d{2})(\d{2})(\d{2})$")


class When:
    """A parsed date. order: 'ymd', 'dmy', 'mdy' (decided by a day > 12 or a month name) or 'ambiguous'."""

    __slots__ = ("d", "alt", "t", "order")

    def __init__(self, d: date, order: str, t: time | None = None, alt: date | None = None) -> None:
        self.d, self.order, self.t, self.alt = d, order, t, alt


def _year(y: str) -> int:
    v = int(y)
    return v + (2000 if v < 70 else 1900) if len(y) == 2 else v


def _time(m: re.Match[str], first: int) -> time | None:
    hh = m.group(first)
    if hh is None:
        return None
    h, mi, se = int(hh), int(m.group(first + 1)), int(m.group(first + 2) or 0)
    ampm = (m.group(first + 3) or "").lower().replace(".", "")
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return time(h, mi, se) if h < 24 and mi < 60 and se < 60 else None


def _mk(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d) if 1000 <= y <= 9999 else None
    except ValueError:
        return None


def parse_date(raw: str, compact: bool = False) -> When | None:
    s = _WEEKDAY.sub("", raw.strip())
    if not s or len(s) > 40:
        return None
    if m := _YMD.match(s):
        d = _mk(int(m[1]), int(m[2]), int(m[3]))
        return When(d, "ymd", _time(m, 4)) if d else None
    if m := _XYZ.match(s):
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        t = _time(m, 4)
        dmy, mdy = _mk(y, b, a), _mk(y, a, b)
        if dmy and mdy:
            return When(dmy, "ambiguous" if a != b else "dmy", t, alt=mdy if a != b else None)
        if dmy:
            return When(dmy, "dmy", t)
        if mdy:
            return When(mdy, "mdy", t)
        return None
    if m := _TEXT_DMY.match(s):
        mo = MONTHS.get(m[2].lower())
        d = _mk(_year(m[3]), mo, int(m[1])) if mo else None
        return When(d, "text", _time(m, 4)) if d else None
    if m := _TEXT_MDY.match(s):
        mo = MONTHS.get(m[1].lower())
        d = _mk(int(m[3]), mo, int(m[2])) if mo else None
        return When(d, "text", _time(m, 4)) if d else None
    if compact and (m := _COMPACT.match(s)):
        d = _mk(int(m[1] + m[2]), int(m[3]), int(m[4]))
        return When(d, "ymd") if d else None
    return None


def date_for(w: When, order: str) -> date:
    return w.alt if w.order == "ambiguous" and order == "mdy" and w.alt else w.d


# ---------------------------------------------------------------- booleans, emails, urls, phones, IBAN

TRUE = frozenset({"true", "yes", "y", "si", "sí", "s", "t", "verdadero", "oui", "ja", "x", "✓", "on"})
FALSE = frozenset({"false", "no", "n", "f", "falso", "non", "nein", "off"})


def parse_bool(raw: str) -> bool | None:
    t = raw.strip().lower()
    return True if t in TRUE else False if t in FALSE else None


_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def parse_email(raw: str) -> str | None:
    s = raw.strip().strip("<>").removeprefix("mailto:")
    if not _EMAIL.match(s) or ".." in s or len(s) > 254:
        return None
    local, _, domain = s.rpartition("@")
    if not re.search(r"\.[A-Za-z]{2,}$", domain):
        return None
    return f"{local}@{domain.lower()}"


_URL = re.compile(r"^(https?://)?([A-Za-z0-9-]+\.)+[A-Za-z]{2,}(:\d{1,5})?(/[^\s]*)?$")


def parse_url(raw: str) -> str | None:
    s = raw.strip()
    if not _URL.match(s) or "@" in s.split("/")[0]:
        return None
    return s if s.lower().startswith(("http://", "https://")) else "https://" + s


CALLING = {"ES": "34", "US": "1", "CA": "1", "GB": "44", "UK": "44", "IE": "353", "FR": "33", "DE": "49",
           "IT": "39", "PT": "351", "NL": "31", "BE": "32", "LU": "352", "CH": "41", "AT": "43", "DK": "45",
           "SE": "46", "NO": "47", "FI": "358", "PL": "48", "CZ": "420", "GR": "30", "RO": "40", "HU": "36",
           "MX": "52", "AR": "54", "CO": "57", "CL": "56", "PE": "51", "BR": "55", "UY": "598", "EC": "593",
           "AU": "61", "NZ": "64", "IN": "91", "SG": "65", "JP": "81", "ZA": "27", "AE": "971", "IL": "972"}
# National numbers that drop a leading trunk 0 after the country code.
TRUNK_ZERO = frozenset({"44", "353", "33", "49", "31", "32", "41", "43", "358", "61", "64", "91", "81", "27",
                        "971", "972", "30", "40", "36", "48", "420", "54", "57", "51", "55", "593", "598"})
NATIONAL_LEN = {"34": (9,), "1": (10,), "33": (9,), "39": tuple(range(6, 12)), "351": (9,), "49": tuple(range(6, 12)),
                "44": (9, 10), "52": (10,), "61": (9,), "31": (9,)}


def parse_phone(raw: str, country: str = "") -> str | None:
    """E.164 ('+34612345678'). Numbers without '+'/00 need `country` (ISO-2). Extensions are dropped."""
    s = re.split(r"(?i)\s*(?:ext\.?|x|#)\s*\d+$", raw.strip())[0]
    if not re.fullmatch(r"[+\d\s().\-/]{6,25}", s):
        return None
    digits = re.sub(r"\D", "", s)
    if s.lstrip().startswith("+"):
        intl = digits
    elif digits.startswith("00"):
        intl = digits[2:]
    else:
        cc = CALLING.get(country.upper())
        if not cc:
            return None
        nat = digits
        if cc in TRUNK_ZERO and nat.startswith("0"):
            nat = nat[1:]
        if cc == "1" and len(nat) == 11 and nat.startswith("1"):
            nat = nat[1:]
        if cc in NATIONAL_LEN and len(nat) not in NATIONAL_LEN[cc]:
            return None
        intl = cc + nat
    for cc in sorted({c for c in CALLING.values() if intl.startswith(c)}, key=len, reverse=True):
        nat = intl[len(cc):]
        if cc in TRUNK_ZERO and nat.startswith("0"):  # '+44 (0)20 ...'
            intl = cc + nat[1:]
            nat = nat[1:]
        if cc in NATIONAL_LEN and len(nat) not in NATIONAL_LEN[cc]:
            return None
        break
    return "+" + intl if 8 <= len(intl) <= 15 and intl[0] != "0" else None


IBAN_LEN = {"AD": 24, "AE": 23, "AT": 20, "BE": 16, "BG": 22, "BR": 29, "CH": 21, "CY": 28, "CZ": 24, "DE": 22,
            "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GI": 23, "GR": 27, "HR": 21, "HU": 28,
            "IE": 22, "IL": 23, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31,
            "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SA": 24, "SE": 24, "SI": 19, "SK": 24, "SM": 27,
            "TR": 26, "UA": 29}


def parse_iban(raw: str) -> str | None:
    s = re.sub(r"[\s\-.]", "", raw.strip().upper()).removeprefix("IBAN")
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return None
    if IBAN_LEN.get(s[:2], len(s)) != len(s):
        return None
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return s if int(num) % 97 == 1 else None


def to_iso(d: date, t: time | None) -> str:
    return datetime.combine(d, t).isoformat() if t else d.isoformat()
