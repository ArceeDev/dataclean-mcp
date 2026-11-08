"""DataClean MCP: deterministic cleaning, type inference and schema validation for CSV, Excel and JSON tables.

LLMs misread European numbers (1.250,50 €), day/month order (03/04/2026), NUL bytes, mojibake and stray spaces.
This server fixes them with rules, never guesses: an ambiguous value is kept and flagged, not converted.

Paid tools debit KeyFlint credits only after the input has been read and processed successfully, and refund them
if the tool fails afterwards. Helper tools are free.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from contextlib import contextmanager
from typing import Any, Iterator

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, schema as S, tables as T
from . import clean as C
from . import values as V
from . import apps
from .billing import Billing
from .regexguard import RegexError

DEFAULT_PRICES = {"profile_table": 1, "clean_table": 1, "validate_schema": 1, "normalize_values": 1}
MAX_BYTES = int(os.environ.get("DATACLEAN_MAX_BYTES", 20_000_000))
MAX_ROWS = int(os.environ.get("DATACLEAN_MAX_ROWS", 200_000))
MAX_OUTPUT = int(os.environ.get("DATACLEAN_MAX_OUTPUT", 15_000_000))
MAX_VALUES = 10_000
PREVIEW = 10


def _prices() -> dict[str, int]:
    prices = dict(DEFAULT_PRICES)
    for item in filter(None, (p.strip() for p in os.environ.get("DATACLEAN_PRICES", "").split(","))):
        name, _, n = item.partition("=")
        if name.strip() in prices and n.strip().isdigit() and int(n) > 0:
            prices[name.strip()] = int(n)
    return prices


PRICES = _prices()
billing = Billing(hosted=os.environ.get("DATACLEAN_TRANSPORT", "stdio") != "stdio")
LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True)

mcp = MCPServer(
    "dataclean-mcp",
    title="DataClean: clean CSV, Excel and JSON tables",
    version=__version__,
    website_url="https://github.com/ArceeDev/dataclean-mcp",
    instructions=(
        "Deterministic data hygiene for tables. Use it BEFORE reasoning over a CSV/Excel file instead of reading raw "
        "cells: `profile_table` reports encoding, delimiter, header, per-column type (integer, decimal, date, "
        "datetime, boolean, email, url, phone, iban, text), number locale (1.234,56 vs 1,234.56), date order "
        "(day-first vs month-first), nulls, duplicates and problems, plus a suggested schema. `clean_table` returns "
        "the table with NUL/control/zero-width characters and mojibake removed, trimmed text, numbers as plain "
        "decimals, dates as ISO 8601, phones as E.164, deduplicated rows, as CSV, JSON, JSONL or typed .xlsx. "
        "`validate_schema` checks rows against a schema (types, required, unique, min/max, enum, pattern). "
        "`normalize_values` converts a list of values. Ambiguous values (03/04/2026, 1,250) are never guessed: "
        "they are converted only when the column settles it or you pass date_order / number_locale, otherwise "
        "kept and flagged. Pass the table as content (text), content_base64 (file bytes, required for .xlsx) or "
        "rows (JSON). Free: `pricing`, `credits_balance`. Unreadable input is never charged; failures are refunded."
    ),
)


UI = apps.ui_meta("dataclean")
apps.register(mcp, slug="dataclean", name="DataClean", icon="table", version=__version__, lang="en",
              buy_url=lambda: billing.checkout_url,
              labels={"profile_table": "Table profile", "clean_table": "Clean table", "validate_schema": "Schema check", "normalize_values": "Normalized values", "credits_balance": "Credit balance"})

@contextmanager
def _user_errors() -> Iterator[None]:
    try:
        yield
    except (T.TableError, C.CleanError, RegexError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _headers(ctx: Context) -> Any:
    try:
        return ctx.headers
    except ValueError:
        return None


def _note(receipt: dict, credits: int) -> dict[str, Any]:
    return {"credits_charged": credits, "credits_remaining": receipt.get("remaining_balance")}


def _opts(number_locale: str, date_order: str, phone_country: str, null_values: list[str] | None,
          types: dict[str, str] | None, min_ratio: float = 0.9, on_invalid: str = "keep",
          fix_mojibake: bool = True) -> C.Options:
    o = C.Options(number_locale=(number_locale or "").lower().strip(), date_order=(date_order or "").lower().strip(),
                  phone_country=(phone_country or "").upper().strip(), min_ratio=min_ratio, on_invalid=on_invalid,
                  null_values=frozenset(str(x).strip().lower() for x in (null_values or [])),
                  fix_mojibake=fix_mojibake, types=dict(types or {}))
    o.check()
    return o


def _load(content: str, content_base64: str, rows: list[Any] | None, delimiter: str, sheet: str,
          has_header: bool, header_row: int) -> T.Table:
    return T.load(content, content_base64, rows, delimiter, sheet, has_header, header_row, MAX_BYTES, MAX_ROWS)


def _source(t: T.Table) -> dict[str, Any]:
    d: dict[str, Any] = {"format": t.source, "rows": len(t.rows), "columns": len(t.headers)}
    for k in ("encoding", "delimiter", "sheet"):
        if getattr(t, k):
            d[k] = getattr(t, k)
    return d


def _flags(t: T.Table, headers: list[str], flags: list[tuple[int, int, Any, str]], cap: int) -> list[dict[str, Any]]:
    return [{"row": t.line_numbers[i], "column": headers[j], "value": T.render(v), "reason": why}
            for i, j, v, why in flags[:cap]]


TABLE_ARGS = """
    Input (exactly one): content (CSV/TSV/JSON text), content_base64 (file bytes: .xlsx, or CSV in any encoding),
    rows (JSON list of objects or of lists). delimiter: auto-detected (, ; tab |). sheet: Excel sheet name (default
    the first). has_header: false when the first row is data. header_row: 1-based row of the header (default auto,
    skipping title rows above it).
    Parsing: number_locale 'eu' (1.234,56) or 'us' (1,234.56) settles values like 1,250 (auto from the column
    otherwise). date_order 'dmy' or 'mdy' settles dates like 03/04/2026 (auto from the column otherwise).
    phone_country (ISO-2, e.g. ES, US, GB) converts national phone numbers to E.164. null_values: extra strings
    read as empty (NULL, N/A, -, #N/A and similar are already). types: force a type per column, e.g.
    {"zip": "text", "amount": "decimal"}; types are text, integer, decimal, boolean, date, datetime, email, url,
    phone, iban."""


def _table_doc(fn: Any) -> Any:
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n" + TABLE_ARGS
    return fn


@mcp.tool(title="Profile table data quality", annotations=LOCAL, meta=UI)
@_table_doc
async def profile_table(ctx: Context, content: str = "", content_base64: str = "", rows: list[Any] | None = None,
                        delimiter: str = "", sheet: str = "", has_header: bool = True, header_row: int = 0,
                        number_locale: str = "", date_order: str = "", phone_country: str = "",
                        null_values: list[str] | None = None, types: dict[str, str] | None = None) -> dict[str, Any]:
    """Profile a table: what each column really contains and what is wrong with it, without changing it.

    Returns the detected encoding/delimiter/header, per-column type, nulls, distinct values, min/max, number
    locale, date order, currency, examples and notes (leading-zero IDs, formula-injection cells, ambiguous dates
    or numbers, mixed separators), table issues (ragged rows, NUL bytes, duplicate headers, title rows), exact
    duplicate rows, the first 50 flagged cells and a suggested schema for validate_schema.
    """
    with _user_errors():
        opts = _opts(number_locale, date_order, phone_country, null_values, types)
        t = await asyncio.to_thread(_load, content, content_base64, rows, delimiter, sheet, has_header, header_row)
    billing.client(_headers(ctx)).check()
    with _user_errors():
        cols, data, flags = await asyncio.to_thread(C.clean_table, t.headers, t.rows, opts)
        _, dups = C.dedupe(t.headers, data, [], False)
    async with billing.charge(PRICES["profile_table"], "profile_table", _headers(ctx)) as receipt:
        pass
    return {"source": _source(t), "issues": t.issues, "duplicate_rows": dups,
            "columns": [c.as_dict() for c in cols], "flags_total": len(flags), "flags": _flags(t, t.headers, flags, 50),
            "suggested_schema": C.suggest_schema(cols, len(t.rows)),
            "billing": _note(receipt, PRICES["profile_table"])}


@mcp.tool(title="Clean and standardize table", annotations=LOCAL, meta=UI)
@_table_doc
async def clean_table(ctx: Context, content: str = "", content_base64: str = "", rows: list[Any] | None = None,
                      output_format: str = "csv", delimiter: str = "", sheet: str = "", has_header: bool = True,
                      header_row: int = 0, number_locale: str = "", date_order: str = "", phone_country: str = "",
                      null_values: list[str] | None = None, types: dict[str, str] | None = None,
                      header_style: str = "keep", dedupe: str = "none", dedupe_keys: list[str] | None = None,
                      drop_empty_columns: bool = False, on_invalid: str = "keep", output_delimiter: str = ",",
                      formula_guard: bool = True, fix_mojibake: bool = True, columns: list[str] | None = None,
                      ) -> dict[str, Any]:
    """Return a clean, typed copy of a table, with a report of every change and every value left unconverted.

    Text: NUL/control/zero-width characters removed, mojibake repaired (CafÃ© -> Café), Unicode NFC, spaces
    collapsed and trimmed. Numbers: plain decimals (1.250,50 € -> 1250.50; (45) -> -45; 1.250,50- -> -1250.50).
    Dates: ISO 8601. Booleans: true/false. Emails: domain lowercased. Phones: E.164. IBAN: compact uppercase.
    Values that do not fit the column type are kept as they were (on_invalid='null' empties them) and listed
    in `flags` with their source row.

    Args:
        output_format: csv (default) | tsv | json | jsonl | xlsx (typed cells; returned only as content_base64).
        header_style: keep | snake (Fecha de alta -> fecha_de_alta). columns: keep only these columns, in order.
        dedupe: none | exact | ignore_case, over all columns or `dedupe_keys`; the first occurrence is kept.
        drop_empty_columns: remove columns with no values. output_delimiter: for csv output.
        formula_guard: prefix text cells starting with = + - @ with ' in CSV so spreadsheets do not run them.
    """
    with _user_errors():
        opts = _opts(number_locale, date_order, phone_country, null_values, types, on_invalid=on_invalid,
                     fix_mojibake=fix_mojibake)
        if output_format not in T.FORMATS:
            raise C.CleanError(f"output_format must be one of {', '.join(T.FORMATS)}")
        if header_style not in ("keep", "snake"):
            raise C.CleanError("header_style must be 'keep' or 'snake'")
        if dedupe not in ("none", "exact", "ignore_case"):
            raise C.CleanError("dedupe must be 'none', 'exact' or 'ignore_case'")
        if len(output_delimiter) != 1:
            raise C.CleanError("output_delimiter must be one character")
        t = await asyncio.to_thread(_load, content, content_base64, rows, delimiter, sheet, has_header, header_row)
    billing.client(_headers(ctx)).check()
    with _user_errors():
        out = await asyncio.to_thread(_clean, t, opts, output_format, header_style, dedupe, dedupe_keys or [],
                                      drop_empty_columns, output_delimiter, formula_guard, columns or [])
    async with billing.charge(PRICES["clean_table"], "clean_table", _headers(ctx)) as receipt:
        pass
    return {**out, "billing": _note(receipt, PRICES["clean_table"])}


def _clean(t: T.Table, opts: C.Options, fmt: str, header_style: str, dedupe: str, keys: list[str],
           drop_empty: bool, out_delim: str, guard: bool, only: list[str]) -> dict[str, Any]:
    cols, data, flags = C.clean_table(t.headers, t.rows, opts)
    headers = list(t.headers)
    removed = 0
    if dedupe != "none":
        keep, removed = C.dedupe(headers, data, keys, dedupe == "ignore_case")
        kept = set(keep)
        data = [data[i] for i in keep]
        index = {old: new for new, old in enumerate(keep)}
        flags = [(index[i], j, v, w) for i, j, v, w in flags if i in kept]
        line_numbers = [t.line_numbers[i] for i in keep]
    else:
        line_numbers = list(t.line_numbers)
    sel = list(range(len(headers)))
    if only:
        missing = [c for c in only if c not in headers]
        if missing:
            raise C.CleanError(f"columns: unknown column(s) {', '.join(missing)}; columns are {', '.join(headers)}")
        sel = [headers.index(c) for c in only]
    dropped = []
    if drop_empty:
        dropped = [headers[j] for j in sel if cols[j].non_null == 0]
        sel = [j for j in sel if cols[j].non_null > 0]
    if not sel:
        raise C.CleanError("no columns left to write")
    final = [headers[j] for j in sel]
    if header_style == "snake":
        snake: list[str] = []
        for h in final:
            s, n = T.to_snake(h), 2
            base = s
            while s in snake:
                s, n = f"{base}_{n}", n + 1
            snake.append(s)
        final = snake
    body = [[r[j] for j in sel] for r in data]
    pos = {j: k for k, j in enumerate(sel)}
    shown = T.Table(final, body, line_numbers, t.source)
    flags = [(i, pos[j], v, w) for i, j, v, w in flags if j in pos]
    result: dict[str, Any] = {"source": _source(t), "issues": t.issues, "rows": len(body),
                              "duplicates_removed": removed, "columns": [cols[j].as_dict() | {"output_name": final[k]}
                                                                         for k, j in enumerate(sel)],
                              "flags_total": len(flags), "flags": _flags(shown, final, flags, C.MAX_FLAGS),
                              "preview": T.records(final, body[:PREVIEW])}
    if dropped:
        result["dropped_empty_columns"] = dropped
    if fmt == "xlsx":
        raw = T.write_xlsx(final, body, t.sheet or "data")
        if len(raw) > MAX_OUTPUT:
            raise C.CleanError(f"output is {len(raw)} bytes; the limit is {MAX_OUTPUT}: filter with `columns`")
        result.update({"format": "xlsx", "filename": "clean.xlsx", "content_base64": base64.b64encode(raw).decode()})
        return result
    if fmt in ("csv", "tsv"):
        text = T.write_csv(final, body, "\t" if fmt == "tsv" else out_delim, guard)
    else:
        text = T.write_json(final, body, lines=fmt == "jsonl")
    if len(text) > MAX_OUTPUT:
        raise C.CleanError(f"output is {len(text)} characters; the limit is {MAX_OUTPUT}: filter with `columns` "
                           "or use profile_table")
    result.update({"format": fmt, "filename": f"clean.{fmt}", "encoding": "utf-8", "content": text})
    if fmt in ("csv", "tsv"):
        result["content_base64"] = base64.b64encode(("﻿" + text).encode("utf-8")).decode()
        result["content_base64_encoding"] = "utf-8-sig (Excel opens accents correctly)"
    return result


@mcp.tool(title="Validate table against schema", annotations=LOCAL, meta=UI)
@_table_doc
async def validate_schema(ctx: Context, schema: dict[str, Any], content: str = "", content_base64: str = "",
                          rows: list[Any] | None = None, delimiter: str = "", sheet: str = "",
                          has_header: bool = True, header_row: int = 0, number_locale: str = "",
                          date_order: str = "", phone_country: str = "", null_values: list[str] | None = None,
                          max_errors: int = 500) -> dict[str, Any]:
    """Check every row of a table against a schema, after the same parsing clean_table applies.

    schema: {"columns": {"id": {"type": "integer", "required": true, "unique": true},
                         "email": {"type": "email", "required": true},
                         "amount": {"type": "decimal", "min": 0, "max": 100000},
                         "signup": {"type": "date", "min": "2020-01-01"},
                         "status": {"enum": ["active", "churned"]},
                         "sku": {"type": "text", "pattern": "[A-Z]{3}-\\\\d{4}", "max_length": 8}},
             "strict_columns": false}
    Rules: type, required, unique (case-insensitive), min/max (numbers and dates), min_length/max_length, enum
    (case-insensitive), pattern (full match; nested quantifiers are refused). Column names match case-insensitively.
    strict_columns flags columns that are not in the schema. Errors carry the source row number; `valid` is
    true only with zero errors. profile_table returns a suggested schema to start from.
    """
    with _user_errors():
        opts = _opts(number_locale, date_order, phone_country, null_values, None)
        t = await asyncio.to_thread(_load, content, content_base64, rows, delimiter, sheet, has_header, header_row)
    billing.client(_headers(ctx)).check()
    with _user_errors():
        res = await asyncio.to_thread(S.validate, t.headers, t.rows, t.line_numbers, schema, opts,
                                      max(1, min(max_errors, 5000)))
    async with billing.charge(PRICES["validate_schema"], "validate_schema", _headers(ctx)) as receipt:
        pass
    return {"source": _source(t), **res, "billing": _note(receipt, PRICES["validate_schema"])}


@mcp.tool(title="Normalize values", annotations=LOCAL, meta=UI)
async def normalize_values(ctx: Context, values: list[Any], kind: str = "auto", number_locale: str = "",
                           date_order: str = "", phone_country: str = "", name: str = "") -> dict[str, Any]:
    """Normalize a list of up to 10,000 values of one kind, e.g. amounts, dates or phone numbers from a document.

    kind: auto (inferred like a table column) | number | integer | decimal | date | datetime | boolean | email |
    url | phone | iban | text. name: optional column name, used as a hint (e.g. 'phone', 'fecha').
    Returns one result per input, in order: {"input", "output", "valid"} and "reason" when it could not be
    converted. Output types: numbers as plain decimal strings, dates ISO 8601, phones E.164.
    """
    with _user_errors():
        if not isinstance(values, list) or not values:
            raise C.CleanError("values must be a non-empty list")
        if len(values) > MAX_VALUES:
            raise C.CleanError(f"at most {MAX_VALUES} values per call")
        kinds = ("auto", "number") + C.KINDS
        if kind not in kinds:
            raise C.CleanError(f"kind must be one of {', '.join(kinds)}")
        opts = _opts(number_locale, date_order, phone_country, None, None)
    billing.client(_headers(ctx)).check()
    with _user_errors():
        forced = "" if kind == "auto" else ("decimal" if kind == "number" else kind)
        col, out, flags = await asyncio.to_thread(C.analyze, name or kind, values, opts, forced)
    reasons = {i: why for i, _, why in flags}
    results = []
    for i, (raw, v) in enumerate(zip(values, out)):
        r: dict[str, Any] = {"input": raw, "output": None if v is None else T.render(v),
                             "valid": v is not None and i not in reasons}
        if i in reasons:
            r["reason"] = reasons[i]
        elif v is None and raw not in (None, ""):
            r["reason"] = "null"
        results.append(r)
    async with billing.charge(PRICES["normalize_values"], "normalize_values", _headers(ctx)) as receipt:
        pass
    return {"kind": col.kind, "column": col.as_dict(), "results": results,
            "billing": _note(receipt, PRICES["normalize_values"])}


@mcp.tool(title="Show tool prices", annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def pricing() -> dict[str, Any]:
    """Credit price of each tool and where to buy credits. Free."""
    return {"credits_per_call": PRICES, "limits": {"bytes": MAX_BYTES, "rows": MAX_ROWS, "values": MAX_VALUES},
            "refunds": "Unreadable or invalid input is never charged; a tool that fails after charging is refunded.",
            "buy_credits": billing.checkout_url or "Set KEYFLINT_CHECKOUT_URL"}


@mcp.tool(title="Check credit balance", annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), meta=UI)
async def credits_balance(ctx: Context) -> dict[str, Any]:
    """Remaining credits on your KeyFlint license. Free."""
    return await billing.balance(_headers(ctx))


_ = V  # parsers are part of the public surface for library users


def main() -> None:
    p = argparse.ArgumentParser(prog="dataclean-mcp", description=__doc__)
    p.add_argument("--http", action="store_true", help="Serve Streamable HTTP (hosted, license per request).")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = p.parse_args()
    if args.http:
        billing.hosted = True
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
