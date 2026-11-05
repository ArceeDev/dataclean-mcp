"""Validate a table against a declared schema, after the same deterministic parsing clean_table uses."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from . import clean as C
from . import values as V
from .regexguard import safe_regex

RULES = ("type", "required", "unique", "min", "max", "min_length", "max_length", "enum", "pattern")


def _bound(kind: str, raw: Any, where: str) -> Any:
    if kind in ("integer", "decimal"):
        try:
            return Decimal(str(raw))
        except InvalidOperation as exc:
            raise C.CleanError(f"{where}: {raw!r} is not a number") from exc
    if kind in ("date", "datetime"):
        w = V.parse_date(str(raw))
        if w is None or w.order == "ambiguous":
            raise C.CleanError(f"{where}: use an ISO date (YYYY-MM-DD), got {raw!r}")
        return datetime.combine(w.d, w.t or time(0))
    raise C.CleanError(f"{where}: min/max only apply to integer, decimal, date and datetime columns")


def _cmp(v: Any) -> Any:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, time(0))
    return v


def validate(headers: list[str], rows: list[list[Any]], line_numbers: list[int], schema: dict[str, Any],
             opts: C.Options, max_errors: int = 500) -> dict[str, Any]:
    if not isinstance(schema, dict) or not isinstance(schema.get("columns"), dict) or not schema["columns"]:
        raise C.CleanError('schema must be {"columns": {"name": {"type": "integer", "required": true, ...}}}')
    specs: dict[str, dict[str, Any]] = schema["columns"]
    lookup = {h.casefold(): h for h in headers}
    errors: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def err(rule: str, column: str, msg: str, row: int | None = None, value: Any = None) -> None:
        counts[f"{column}:{rule}"] = counts.get(f"{column}:{rule}", 0) + 1
        if len(errors) < max_errors:
            e: dict[str, Any] = {"rule": rule, "column": column, "message": msg}
            if row is not None:
                e["row"] = row
            if value is not None:
                e["value"] = value if isinstance(value, str) else str(value)
            errors.append(e)

    plan: list[tuple[str, int, dict[str, Any], Any, Any, re.Pattern[str] | None, set[str] | None]] = []
    for name, spec in specs.items():
        if not isinstance(spec, dict):
            raise C.CleanError(f"schema column {name!r} must be an object")
        bad = [k for k in spec if k not in RULES]
        if bad:
            raise C.CleanError(f"schema column {name!r}: unknown rule(s) {', '.join(bad)}; valid: {', '.join(RULES)}")
        kind = spec.get("type", "text")
        if kind not in C.KINDS:
            raise C.CleanError(f"schema column {name!r}: type must be one of {', '.join(C.KINDS)}")
        actual = lookup.get(name.casefold())
        if actual is None:
            if spec.get("required"):
                err("missing_column", name, f"required column {name!r} is missing")
            continue
        lo = _bound(kind, spec["min"], f"{name}.min") if "min" in spec else None
        hi = _bound(kind, spec["max"], f"{name}.max") if "max" in spec else None
        pattern = safe_regex(str(spec["pattern"]), f"{name}.pattern") if spec.get("pattern") else None
        enum = {str(x).casefold() for x in spec["enum"]} if isinstance(spec.get("enum"), list) else None
        plan.append((name, headers.index(actual), spec, lo, hi, pattern, enum))
    if schema.get("strict_columns"):
        known = {n.casefold() for n in specs}
        for h in headers:
            if h.casefold() not in known:
                err("unexpected_column", h, f"column {h!r} is not in the schema")

    columns: list[dict[str, Any]] = []
    for name, j, spec, lo, hi, pattern, enum in plan:
        kind = spec.get("type", "text")
        col, out, flags = C.analyze(name, [r[j] for r in rows], opts, forced=kind)
        columns.append(col.as_dict())
        for i, v, why in flags:
            err("type", name, f"not a valid {kind} ({why})", line_numbers[i], v)
        bad_rows = {i for i, _, _ in flags}
        seen: dict[str, int] = {}
        for i, v in enumerate(out):
            line = line_numbers[i]
            if v is None:
                if spec.get("required"):
                    err("required", name, "value is required", line)
                continue
            if i in bad_rows:
                continue
            text = str(v)
            if spec.get("unique"):
                key = text.casefold()
                if key in seen:
                    err("unique", name, f"duplicate of row {seen[key]}", line, v)
                else:
                    seen[key] = line
            if lo is not None and _cmp(v) < lo:
                err("min", name, f"below minimum {spec['min']}", line, v)
            if hi is not None and _cmp(v) > hi:
                err("max", name, f"above maximum {spec['max']}", line, v)
            if "min_length" in spec and len(text) < int(spec["min_length"]):
                err("min_length", name, f"shorter than {spec['min_length']} characters", line, v)
            if "max_length" in spec and len(text) > int(spec["max_length"]):
                err("max_length", name, f"longer than {spec['max_length']} characters", line, v)
            if enum is not None and text.casefold() not in enum:
                err("enum", name, "not one of the allowed values", line, v)
            if pattern is not None and not pattern.fullmatch(text[:10_000]):
                err("pattern", name, "does not match the pattern", line, v)
    total = sum(counts.values())
    return {"valid": total == 0, "errors_total": total, "errors_by_rule": counts, "errors": errors,
            "errors_truncated": total > len(errors), "rows": len(rows), "columns": columns}
