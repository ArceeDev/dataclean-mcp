---
name: dataclean-tables
description: Clean, profile and validate CSV, Excel or JSON tables before analysing them. Use when a spreadsheet has European or US formatted numbers (1.250,50 €), mixed date formats (03/04/2026), stray spaces, NUL bytes, mojibake, duplicates, phone numbers to normalize, or must be checked against a schema before import.
---

# DataClean: deterministic table cleaning

Needs the `dataclean-mcp` server. Each paid tool costs 1 KeyFlint credit. Bad input is never charged, and failures are refunded.

## Workflow

1. **`profile_table`** first, on any file you did not produce. Read `columns[].type`, `number_locale`, `date_order` and `notes`, plus `issues` and `flags`.
2. If the notes mention ambiguous dates or numbers, **ask the user** (or use what you know about the source: a Spanish bank uses `dmy` and `eu`), then pass `date_order` / `number_locale`. Never pick one silently.
3. **`clean_table`** with those parameters. Use `output_format="json"` to reason over the rows, or `xlsx`/`csv` to give the user a file. Report `flags_total` and the reasons.
4. Before an import, run **`validate_schema`**. Start from `profile_table`'s `suggested_schema` and tighten it (enum, min/max, pattern).

## Tips

- Pass .xlsx and non-UTF-8 CSV files as `content_base64`. Pass JSON rows through `rows`.
- Use `types={"zip": "text"}` to stop a column from being converted.
- Use `phone_country` (ISO-2) to convert national phone numbers to E.164.
- `dedupe="ignore_case"` together with `dedupe_keys` removes duplicate customers by email.
- Amounts come back as plain decimals that keep their scale (`1250.50`). Do the arithmetic on those values, not on the original text.
