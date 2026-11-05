"""Column type inference, cleaning and schema validation. Deterministic: same input, same output.

A value is converted only when its meaning is certain. '03/04/2026' becomes a date only if another value in the
column (e.g. '13/04/2026') or `date_order` settles day-first vs month-first; '1,250' becomes a number only if the
column or `number_locale` settles whether the comma is a thousands or a decimal separator. Otherwise the value is
kept as it was and flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable

from . import values as V

KINDS = ("text", "integer", "decimal", "boolean", "date", "datetime", "email", "url", "phone", "iban")
MAX_FLAGS = 500


class CleanError(ValueError):
    pass


@dataclass
class Options:
    number_locale: str = ""     # '', 'eu', 'us'
    date_order: str = ""        # '', 'dmy', 'mdy'
    phone_country: str = ""     # ISO-2 for numbers without +/00
    min_ratio: float = 0.9      # share of non-null values that must parse for a column to get a type
    null_values: frozenset[str] = frozenset()
    on_invalid: str = "keep"    # keep the original text, or 'null'
    collapse_spaces: bool = True
    fix_mojibake: bool = True
    types: dict[str, str] = field(default_factory=dict)  # forced kinds by column name

    def check(self) -> None:
        if self.number_locale not in ("", "eu", "us"):
            raise CleanError("number_locale must be 'eu' (1.234,56), 'us' (1,234.56) or empty for auto")
        if self.date_order not in ("", "dmy", "mdy"):
            raise CleanError("date_order must be 'dmy', 'mdy' or empty for auto")
        if self.phone_country and self.phone_country.upper() not in V.CALLING:
            raise CleanError(f"phone_country {self.phone_country!r} not supported; use one of "
                             f"{', '.join(sorted(V.CALLING))}")
        if not 0.5 <= self.min_ratio <= 1:
            raise CleanError("min_ratio must be between 0.5 and 1")
        if self.on_invalid not in ("keep", "null"):
            raise CleanError("on_invalid must be 'keep' or 'null'")
        for col, kind in self.types.items():
            if kind not in KINDS:
                raise CleanError(f"types[{col!r}]: {kind!r} is not one of {', '.join(KINDS)}")


@dataclass
class Column:
    name: str
    kind: str = "text"
    non_null: int = 0
    nulls: int = 0
    distinct: int = 0
    converted: int = 0
    invalid: int = 0
    number_locale: str = ""
    date_order: str = ""
    currency: str = ""
    percent: bool = False
    notes: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {"name": self.name, "type": self.kind, "non_null": self.non_null, "nulls": self.nulls,
             "distinct": self.distinct, "converted": self.converted, "invalid": self.invalid,
             "examples": self.examples}
        for k in ("number_locale", "date_order", "currency"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.percent:
            d["percent"] = True
        if self.stats:
            d["stats"] = self.stats
        if self.notes:
            d["notes"] = self.notes
        return d


_PHONE_HINT = re.compile(r"phone|tel[eé]?f|^tel$|m[oó]vil|mobile|cell|whatsapp|fax|celular", re.I)
_DATE_HINT = re.compile(r"date|fecha|datum|data|day|d[ií]a|created|updated|_at$|time|born|birth|nacim", re.I)


def _prep(v: Any, opts: Options) -> Any:
    """Raw cell -> None, a native typed value (from Excel/JSON) or cleaned text."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, Decimal)):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v)) if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, datetime):
        return v.date() if v.time() == time(0) else v.replace(tzinfo=None)
    if isinstance(v, (date, time)):
        return v
    s = V.clean_text(str(v), opts.collapse_spaces, opts.fix_mojibake)
    return None if V.is_null(s, opts.null_values) else s


def _parsers(name: str, opts: Options) -> dict[str, Callable[[str], Any]]:
    compact = bool(_DATE_HINT.search(name))
    return {
        "boolean": V.parse_bool,
        "number": V.parse_number,
        "date": lambda s: V.parse_date(s, compact),
        "email": V.parse_email,
        "iban": V.parse_iban,
        "phone": lambda s: V.parse_phone(s, opts.phone_country),
        "url": V.parse_url,
    }


def _native_kind(v: Any) -> str | None:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, Decimal):
        return "number"
    if isinstance(v, (date, datetime)):
        return "date"
    return None


def _infer(name: str, vals: list[Any], opts: Options) -> str:
    if not vals:
        return "empty"
    parsers = _parsers(name, opts)
    texts = [v for v in vals if isinstance(v, str)]
    sample = texts[:5000]
    natives: dict[str, int] = {}
    for v in vals:
        k = _native_kind(v)
        if k:
            natives[k] = natives.get(k, 0) + 1
    n = len(vals)
    step = max(1, len(texts) // max(1, len(sample)))
    scores: dict[str, float] = {}
    for kind, fn in parsers.items():
        if kind == "phone" and not (_PHONE_HINT.search(name) or
                                    sum(1 for s in sample if s.startswith(("+", "00"))) > 0.8 * max(1, len(sample))):
            continue
        hits = sum(1 for s in sample if fn(s) is not None) * step
        scores[kind] = (hits + natives.get(kind, 0)) / n
    order = ("boolean", "date", "phone", "number", "email", "iban", "url")
    best = max(order, key=lambda k: (scores.get(k, 0), -order.index(k)))
    if scores.get(best, 0) < opts.min_ratio:
        return "text"
    if best == "number":
        plain = [s.lstrip("+-") for s in texts if re.fullmatch(r"[+-]?\d+", s)]
        if any(len(s) > 1 and s.startswith("0") for s in plain) or any(len(s) > 15 for s in plain):
            return "identifier"
    return best


def analyze(name: str, cells: list[Any], opts: Options, forced: str = "") -> tuple[Column, list[Any],
                                                                                 list[tuple[int, Any, str]]]:
    """Return (column report, cleaned values, flags as (row index, original, reason))."""
    col = Column(name)
    prepped = [_prep(v, opts) for v in cells]
    vals = [v for v in prepped if v is not None]
    col.nulls, col.non_null = len(prepped) - len(vals), len(vals)
    kind = forced or opts.types.get(name, "")
    if kind in ("integer", "decimal"):
        kind = "number:" + kind
    elif kind == "datetime":
        kind = "date:datetime"
    elif not kind:
        kind = _infer(name, vals, opts)
        if kind == "identifier":
            col.notes.append("digits with leading zeros or over 15 digits: kept as text (an ID, not a number)")
            kind = "text"
    flags: list[tuple[int, Any, str]] = []
    out: list[Any] = list(prepped)
    base = kind.split(":")[0]
    if base == "empty":
        col.kind = "text"
        col.notes.append("column is empty")
    elif base == "number":
        _numbers(col, prepped, out, flags, opts, kind.partition(":")[2])
    elif base == "date":
        _dates(col, name, prepped, out, flags, opts, kind.partition(":")[2])
    elif base == "text":
        col.kind = "text"
        lengths = [len(v) for v in vals if isinstance(v, str)]
        if lengths:
            col.stats = {"min_length": min(lengths), "max_length": max(lengths)}
        formula = sum(1 for v in vals if isinstance(v, str) and v[:1] in "=+-@" and V.parse_number(v) is None)
        if formula:
            col.notes.append(f"{formula} value(s) start with = + - @ (spreadsheet formula injection risk)")
        out = [render_text(v) for v in prepped]
    else:
        col.kind = base
        fn = _parsers(name, opts)[base]
        for i, v in enumerate(prepped):
            if v is None:
                continue
            if base == "boolean" and isinstance(v, bool):
                continue
            got = fn(str(v)) if not isinstance(v, bool) else None
            if got is None:
                _invalid(col, i, v, f"invalid_{base}", out, flags, opts)
            else:
                out[i] = got
                col.converted += got != v
        if base == "phone" and not opts.phone_country and col.invalid:
            col.notes.append("set phone_country to convert national numbers without + or 00")
    col.distinct = len({str(v) for v in out if v is not None})
    seen: list[str] = []
    for v in vals:
        s = str(v)
        if s not in seen:
            seen.append(s)
        if len(seen) == 3:
            break
    col.examples = seen
    return col, out, flags


def render_text(v: Any) -> Any:
    if v is None or isinstance(v, str):
        return v
    from .tables import render

    return render(v)


def _invalid(col: Column, i: int, v: Any, reason: str, out: list[Any], flags: list[tuple[int, Any, str]],
             opts: Options) -> None:
    col.invalid += 1
    flags.append((i, v, reason))
    out[i] = None if opts.on_invalid == "null" else render_text(v)


def _numbers(col: Column, prepped: list[Any], out: list[Any], flags: list[tuple[int, Any, str]], opts: Options,
             forced: str) -> None:
    parsed: dict[int, V.Num] = {}
    eu = us = 0
    currencies: dict[str, int] = {}
    pct = 0
    for i, v in enumerate(prepped):
        if v is None or isinstance(v, Decimal):
            continue
        n = V.parse_number(str(v)) if not isinstance(v, bool) else None
        if n is None:
            _invalid(col, i, v, "invalid_number", out, flags, opts)
            continue
        parsed[i] = n
        eu += n.locale == "eu"
        us += n.locale == "us"
        pct += n.percent
        if n.currency:
            currencies[n.currency] = currencies.get(n.currency, 0) + 1
    locale = opts.number_locale or ("eu" if eu > us else "us" if us > eu else "")
    if eu and us:
        col.notes.append(f"mixed separators: {eu} value(s) like 1.234,56 and {us} like 1,234.56")
    col.number_locale = locale
    unresolved = 0
    nums: list[Decimal] = []
    for i, v in enumerate(prepped):
        if isinstance(v, Decimal):
            out[i] = v
            nums.append(v)
            continue
        n = parsed.get(i)
        if n is None:
            continue
        val = V.number_for(n, locale)
        if val is None:
            unresolved += 1
            _invalid(col, i, v, "ambiguous_number", out, flags, opts)
            continue
        out[i] = val
        nums.append(val)
        col.converted += 1
    if unresolved:
        col.notes.append(f"{unresolved} value(s) like 1,250 could be thousands or decimals: set number_locale")
    if currencies:
        col.currency = max(currencies, key=currencies.get)  # type: ignore[arg-type]
        if len(currencies) > 1:
            col.notes.append(f"several currencies: {', '.join(sorted(currencies))}")
    col.percent = pct > 0
    integral = all(x == x.to_integral_value() for x in nums)
    decimal_mark = any(("," in str(prepped[i])) if n.locale == "eu" else ("." in str(prepped[i]))
                       for i, n in parsed.items() if n.locale in ("eu", "us"))
    col.kind = forced or ("integer" if integral and not decimal_mark else "decimal")
    if col.kind == "integer":
        for i, v in enumerate(out):
            if isinstance(v, Decimal) and v != v.to_integral_value():
                _invalid(col, i, prepped[i], "invalid_integer", out, flags, opts)
    if nums:
        col.stats = {"min": V.canonical(min(nums)), "max": V.canonical(max(nums)), "sum": V.canonical(sum(nums))}


def _dates(col: Column, name: str, prepped: list[Any], out: list[Any], flags: list[tuple[int, Any, str]],
           opts: Options, forced: str) -> None:
    compact = bool(_DATE_HINT.search(name)) or bool(forced) or bool(opts.types.get(name))
    parsed: dict[int, V.When] = {}
    dmy = mdy = 0
    for i, v in enumerate(prepped):
        if v is None or isinstance(v, (date, datetime)):
            continue
        w = V.parse_date(str(v), compact)
        if w is None:
            _invalid(col, i, v, "invalid_date", out, flags, opts)
            continue
        parsed[i] = w
        dmy += w.order == "dmy"
        mdy += w.order == "mdy"
    order = opts.date_order or ("dmy" if dmy > mdy else "mdy" if mdy > dmy else "")
    if dmy and mdy:
        col.notes.append(f"day-first and month-first dates mixed ({dmy} vs {mdy}); minority flagged")
    col.date_order = order
    unresolved = 0
    has_time = False
    got: list[date] = []
    for i, v in enumerate(prepped):
        if isinstance(v, (date, datetime)):
            has_time |= isinstance(v, datetime)
            got.append(v)
            continue
        w = parsed.get(i)
        if w is None:
            continue
        if w.order == "ambiguous" and not order:
            unresolved += 1
            _invalid(col, i, v, "ambiguous_date", out, flags, opts)
            continue
        if order and w.order in ("dmy", "mdy") and w.order != order:
            _invalid(col, i, v, "date_order_conflict", out, flags, opts)
            continue
        d = V.date_for(w, order)
        val: date = datetime.combine(d, w.t) if w.t else d
        has_time |= w.t is not None
        out[i] = val
        got.append(val)
        col.converted += 1
    if unresolved:
        col.notes.append(f"{unresolved} date(s) like 03/04/2026 could be day-first or month-first: set date_order")
    col.kind = forced or ("datetime" if has_time else "date")
    if got:
        keys = [g if isinstance(g, datetime) else datetime.combine(g, time(0)) for g in got]
        col.stats = {"min": min(keys).isoformat(), "max": max(keys).isoformat()}
        if col.kind == "date":
            col.stats = {k: v[:10] for k, v in col.stats.items()}


# ---------------------------------------------------------------- whole table

def clean_table(headers: list[str], rows: list[list[Any]], opts: Options) -> tuple[list[Column], list[list[Any]],
                                                                                    list[tuple[int, int, Any, str]]]:
    opts.check()
    unknown = [c for c in opts.types if c not in headers]
    if unknown:
        raise CleanError(f"types: unknown column(s) {', '.join(unknown)}; columns are {', '.join(headers)}")
    cols: list[Column] = []
    data: list[list[Any]] = [[None] * len(headers) for _ in rows]
    flags: list[tuple[int, int, Any, str]] = []
    for j, name in enumerate(headers):
        col, out, fl = analyze(name, [r[j] for r in rows], opts)
        cols.append(col)
        for i, v in enumerate(out):
            data[i][j] = v
        flags.extend((i, j, v, why) for i, v, why in fl)
    flags.sort()
    return cols, data, flags


def dedupe(headers: list[str], rows: list[list[Any]], keys: list[str], ignore_case: bool) -> tuple[list[int], int]:
    """Indexes of rows to keep (first occurrence wins) and how many were removed."""
    missing = [k for k in keys if k not in headers]
    if missing:
        raise CleanError(f"dedupe_keys: unknown column(s) {', '.join(missing)}")
    idx = [headers.index(k) for k in keys] if keys else list(range(len(headers)))
    seen: set[tuple[str, ...]] = set()
    keep: list[int] = []
    for i, r in enumerate(rows):
        key = tuple((str(r[j]).casefold() if ignore_case and r[j] is not None else str(r[j])) for j in idx)
        if key in seen:
            continue
        seen.add(key)
        keep.append(i)
    return keep, len(rows) - len(keep)


def suggest_schema(cols: list[Column], rows_total: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in cols:
        spec: dict[str, Any] = {"type": c.kind}
        if c.nulls == 0 and c.non_null:
            spec["required"] = True
        if c.non_null > 1 and c.distinct == c.non_null and c.kind in ("text", "integer", "email", "iban", "phone"):
            spec["unique"] = True
        out[c.name] = spec
    return {"columns": out}
