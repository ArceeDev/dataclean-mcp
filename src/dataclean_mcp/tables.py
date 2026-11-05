"""Reading CSV/TSV/Excel/JSON tables into rows of cells, and writing cleaned tables back out."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .values import canonical, clean_text, parse_number

FORMATS = ("csv", "tsv", "json", "jsonl", "xlsx")
csv.field_size_limit(10_000_000)


class TableError(ValueError):
    pass


@dataclass
class Table:
    headers: list[str]
    rows: list[list[Any]]
    line_numbers: list[int]
    source: str
    encoding: str = ""
    delimiter: str = ""
    sheet: str = ""
    issues: list[str] = field(default_factory=list)
    original_headers: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- decoding

def decode(raw: bytes) -> tuple[str, str, int]:
    """Return (text, encoding, NUL characters removed)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        text, enc = raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text, enc = raw.decode("utf-16", "replace"), "utf-16"
    elif len(raw) > 8 and raw[1:200:2].count(0) > 0.8 * len(raw[1:200:2]):
        text, enc = raw.decode("utf-16-le", "replace"), "utf-16-le"
    elif len(raw) > 8 and raw[0:200:2].count(0) > 0.8 * len(raw[0:200:2]):
        text, enc = raw.decode("utf-16-be", "replace"), "utf-16-be"
    else:
        try:
            text, enc = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, enc = raw.decode("cp1252", "replace"), "cp1252"
    nuls = text.count("\x00")
    return text.replace("\x00", ""), enc, nuls


# ---------------------------------------------------------------- CSV

def sniff(text: str) -> str:
    sample = text[:65536].splitlines()[:50]
    lines = [ln for ln in sample if ln.strip()]
    joined = "\n".join(sample)
    # Single-column file: any delimiter that never appears keeps each line whole.
    best, score = next((d for d in (",", "\t", ";", "|") if d not in joined), ","), -1.0
    for d in (",", ";", "\t", "|"):
        if d == "," and lines and "," not in lines[0] and all(
                parse_number(ln) is not None for ln in lines[1:] if "," in ln):
            continue  # one column of decimal-comma numbers, not a comma-separated file
        try:
            widths = [len(r) for r in csv.reader(sample, delimiter=d) if r]
        except csv.Error:
            continue
        if not widths:
            continue
        multi = [w for w in widths if w > 1]
        if not multi:
            continue
        mode = max(sorted(set(multi)), key=multi.count)
        s = widths.count(mode) / len(widths) + mode / 1000
        if s > score:
            best, score = d, s
    return best


def _header_index(rows: list[list[Any]]) -> int:
    """First row that looks like a header: at least half as many filled cells as the widest of the next rows."""
    widths = [sum(1 for c in r if str(c if c is not None else "").strip()) for r in rows[:30]]
    if not widths:
        return 0
    target = max(widths)
    for i, w in enumerate(widths):
        if w >= max(2, target * 0.5) or w == target:
            return i
    return 0


def _unique_headers(raw: list[Any], issues: list[str]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    blanks = dups = 0
    for i, h in enumerate(raw, 1):
        name = clean_text(str(h)) if h is not None else ""
        if not name:
            name, blanks = f"column_{i}", blanks + 1
        key = name.lower()
        if key in seen:
            seen[key] += 1
            dups += 1
            name = f"{name}_{seen[key]}"
            key = name.lower()
        seen.setdefault(key, 1)
        out.append(name)
    if blanks:
        issues.append(f"{blanks} empty header(s) named column_N")
    if dups:
        issues.append(f"{dups} duplicate header(s) suffixed _2, _3…")
    return out


def _build(rows: list[list[Any]], numbers: list[int], has_header: bool, header_row: int, source: str,
           max_rows: int, issues: list[str]) -> Table:
    keep = [(r, n) for r, n in zip(rows, numbers) if any(str(c if c is not None else "").strip() for c in r)]
    if len(keep) < len(rows):
        issues.append(f"{len(rows) - len(keep)} blank row(s) dropped")
    if not keep:
        raise TableError("the table is empty")
    rows, numbers = [r for r, _ in keep], [n for _, n in keep]
    if has_header:
        hi = header_row - 1 if header_row > 0 else _header_index(rows)
        if hi >= len(rows):
            raise TableError(f"header_row {header_row} is past the end of the table")
        if hi:
            issues.append(f"{hi} row(s) above the header skipped (titles or notes)")
        raw_head, body, numbers = rows[hi], rows[hi + 1:], numbers[hi + 1:]
    else:
        width = max(len(r) for r in rows)
        raw_head, body = [f"column_{i}" for i in range(1, width + 1)], rows
    if len(body) > max_rows:
        raise TableError(f"the table has {len(body)} rows; the limit is {max_rows}")
    width = max([len(raw_head)] + [len(r) for r in body])
    short = sum(1 for r in body if len(r) < width)
    long_ = sum(1 for r in body if len(r) > len(raw_head))
    if long_:
        issues.append(f"{long_} row(s) longer than the header: extra cells kept as extra_N columns")
        raw_head = list(raw_head) + [f"extra_{i}" for i in range(1, width - len(raw_head) + 1)]
    if short and short != len(body) or (short and long_):
        issues.append(f"{short} row(s) shorter than the header padded with empty cells")
    body = [list(r) + [None] * (width - len(r)) for r in body]
    original = [str(h) if h is not None else "" for h in raw_head]
    return Table(_unique_headers(raw_head, issues), body, numbers, source, issues=issues, original_headers=original)


def read_csv(text: str, delimiter: str = "", has_header: bool = True, header_row: int = 0,
             max_rows: int = 200_000) -> Table:
    if text.startswith("﻿"):
        text = text[1:]
    d = delimiter or sniff(text)
    if len(d) != 1:
        raise TableError("delimiter must be one character")
    rows, numbers = [], []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=d)
    try:
        for r in reader:
            rows.append(r)
            numbers.append(reader.line_num)
            if len(rows) > max_rows + 1000:
                raise TableError(f"the table has more than {max_rows} rows")
    except csv.Error as exc:
        raise TableError(f"malformed CSV at line {reader.line_num}: {exc}") from exc
    t = _build(rows, numbers, has_header, header_row, "tsv" if d == "\t" else "csv", max_rows, [])
    t.delimiter = d
    return t


# ---------------------------------------------------------------- Excel and JSON

def read_xlsx(raw: bytes, sheet: str = "", has_header: bool = True, header_row: int = 0,
              max_rows: int = 200_000) -> Table:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise TableError("Excel support needs openpyxl") from exc
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - corrupt or not a workbook
        raise TableError(f"not a readable .xlsx workbook ({type(exc).__name__})") from exc
    try:
        if sheet:
            if sheet not in wb.sheetnames:
                raise TableError(f"sheet {sheet!r} not found; sheets: {', '.join(wb.sheetnames)}")
            ws = wb[sheet]
        else:
            ws = wb.worksheets[0]
        rows, numbers = [], []
        for i, r in enumerate(ws.iter_rows(values_only=True), 1):
            rows.append(list(r))
            numbers.append(i)
            if len(rows) > max_rows + 1000:
                raise TableError(f"the sheet has more than {max_rows} rows")
        issues = [f"sheet {ws.title!r} of {len(wb.sheetnames)}"] if len(wb.sheetnames) > 1 else []
        name = ws.title
    finally:
        wb.close()
    while rows and rows[-1] and all(c is None for c in rows[-1]):
        rows.pop()
    rows = [list(r) for r in rows]
    t = _build(rows, numbers, has_header, header_row, "xlsx", max_rows, issues)
    t.sheet = name
    return t


def read_records(records: list[Any], has_header: bool = True, max_rows: int = 200_000) -> Table:
    if not records:
        raise TableError("rows is empty")
    if all(isinstance(r, dict) for r in records):
        head: dict[str, None] = {}
        for r in records:
            head.update(dict.fromkeys(map(str, r)))
        cols = list(head)
        body = [[_jsonable(r.get(c)) for c in cols] for r in records]
        if len(body) > max_rows:
            raise TableError(f"the table has {len(body)} rows; the limit is {max_rows}")
        return Table(_unique_headers(cols, []), body, list(range(1, len(body) + 1)), "json",
                     original_headers=cols)
    if all(isinstance(r, list) for r in records):
        rows = [[_jsonable(c) for c in r] for r in records]
        return _build(rows, list(range(1, len(rows) + 1)), has_header, 1 if has_header else 0, "json", max_rows, [])
    raise TableError("rows must be a list of objects or a list of lists")


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return Decimal(repr(v))
    return v


def load(content: str = "", content_base64: str = "", rows: list[Any] | None = None, delimiter: str = "",
         sheet: str = "", has_header: bool = True, header_row: int = 0, max_bytes: int = 20_000_000,
         max_rows: int = 200_000) -> Table:
    given = sum(bool(x) for x in (content, content_base64, rows))
    if given != 1:
        raise TableError("pass the table once: as content (text), content_base64 (file bytes) or rows (JSON)")
    if rows:
        return read_records(rows, has_header, max_rows)
    nuls = 0
    if content_base64:
        try:
            raw = base64.b64decode("".join(content_base64.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TableError("content_base64 is not valid base64") from exc
        if len(raw) > max_bytes:
            raise TableError(f"file is {len(raw)} bytes; the limit is {max_bytes}")
        if raw[:4] == b"PK\x03\x04":
            return read_xlsx(raw, sheet, has_header, header_row, max_rows)
        if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise TableError("old .xls (Excel 97-2003) is not supported: save it as .xlsx or CSV")
        text, enc, nuls = decode(raw)
    else:
        if len(content) > max_bytes:
            raise TableError(f"content is {len(content)} characters; the limit is {max_bytes}")
        text, enc = content, "text"
        nuls = text.count("\x00")
        text = text.replace("\x00", "")
    if not text.strip():
        raise TableError("the file is empty")
    stripped = text.lstrip("﻿ \r\n\t")
    if stripped[:1] in "[{" and not delimiter:
        try:
            data = json.loads(stripped) if stripped[0] == "[" else [json.loads(x) for x in stripped.splitlines()
                                                                     if x.strip()]
        except ValueError:
            data = None
        if isinstance(data, list) and data:
            t = read_records(data, has_header, max_rows)
            t.encoding = enc
            return t
    t = read_csv(text, delimiter, has_header, header_row, max_rows)
    t.encoding = enc
    if nuls:
        t.issues.append(f"{nuls} NUL character(s) removed")
    return t


# ---------------------------------------------------------------- writing

def render(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return canonical(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


_FORMULA = ("=", "+", "-", "@", "\t", "\r")


_NUMERIC_TEXT = re.compile(r"^[+-]?[\d\s().]+$")  # E.164 phones and signed codes are not formulas


def _guard(v: Any, on: bool) -> str:
    s = render(v)
    if on and isinstance(v, str) and s[:1] in _FORMULA and not _NUMERIC_TEXT.match(s):
        return "'" + s
    return s


def write_csv(headers: list[str], rows: list[list[Any]], delimiter: str = ",", formula_guard: bool = True) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow([_guard(h, formula_guard) for h in headers])
    for r in rows:
        w.writerow([_guard(v, formula_guard) for v in r])
    return buf.getvalue()


def _json_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        s = canonical(v)
        if len(s.replace("-", "").replace(".", "")) <= 15:
            return int(s) if "." not in s else float(s)
        return s
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def records(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [{h: _json_value(v) for h, v in zip(headers, r)} for r in rows]


def write_json(headers: list[str], rows: list[list[Any]], lines: bool = False) -> str:
    recs = records(headers, rows)
    if lines:
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    return json.dumps(recs, ensure_ascii=False, indent=None)


def write_xlsx(headers: list[str], rows: list[list[Any]], sheet: str = "data") -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import TYPE_STRING

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "_", sheet)[:31] or "data")
    ws.append(headers)
    for r in rows:
        out = []
        for v in r:
            if isinstance(v, Decimal):
                out.append(int(v) if v == v.to_integral_value() and abs(v) < 10 ** 15 else float(v))
            elif isinstance(v, str):
                from openpyxl.cell import WriteOnlyCell

                c = WriteOnlyCell(ws, value=v)
                c.data_type = TYPE_STRING  # never a formula, even when it starts with '='
                out.append(c)
            else:
                out.append(v)
        ws.append(out)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_snake(name: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    return s or "column"
