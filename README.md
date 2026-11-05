# dataclean-mcp

An MCP server that cleans tables **deterministically** before an AI agent reasons over them. LLMs misread `1.250,50 €`, swap day and month in `03/04/2026`, choke on NUL bytes and mojibake (`CafÃ©`), and silently drop leading zeros. DataClean fixes these with rules, and it never guesses: an ambiguous value is kept as it was and flagged, and it is converted only when the rest of its column (or a parameter you pass) settles its meaning.

| Tool | Credits | What it does |
| --- | --- | --- |
| `profile_table` | 1 | Encoding, delimiter, header row, and for each column: real type (integer, decimal, date, datetime, boolean, email, url, phone, iban, text), number locale, date order, currency, nulls, distinct values, min/max, examples and problems. Also reports duplicate rows and a suggested schema. |
| `clean_table` | 1 | Returns a clean, typed copy as CSV, TSV, JSON, JSONL or **typed .xlsx**: text trimmed and repaired, numbers as plain decimals, dates in ISO 8601, phones in E.164, IBANs compacted, rows deduplicated, headers made unique or snake_case. Every value left unconverted is listed with its source row. |
| `validate_schema` | 1 | Checks every row against a schema (type, required, unique, min/max, min_length/max_length, enum, pattern) and returns errors with their row numbers. |
| `normalize_values` | 1 | Normalizes a list of up to 10,000 values (amounts, dates, phones, emails…) taken from a document or another tool. |
| `pricing`, `credits_balance` | 0 | Free helpers. |

Charging works like this:
- Unreadable input or invalid parameters are refused **before** any credits are charged.
- A failure after charging is refunded.

## What it fixes

- **Numbers**: `1.250,50 €`, `$1,250.50`, `1 234,5`, `1'234.50`, `(45.30)`, SAP-style `1.250,50-`, `12,5 %`, `EUR 12`. The column decides whether a value like `1,250` uses a thousands or a decimal separator. If nothing in the column decides it, `number_locale` does, and without either the value is flagged `ambiguous_number`.
- **Dates**: ISO, `13/04/2026` (day-first), `04/13/2026` (month-first), `5 de marzo de 2026`, `March 5, 2026`, `5-Mar-26`, `3. März 2026`, and times with am/pm. `03/04/2026` is converted only when another value in the column, or `date_order`, settles the order. Dates in the minority order are flagged `date_order_conflict`.
- **Text**: removes NUL, control and zero-width characters, turns NBSP into a normal space, repairs mojibake, applies Unicode NFC, and collapses and trims spaces. Null markers (`NULL`, `N/A`, `#N/A`, `-`, `nan`…) become empty.
- **IDs**: digit strings with leading zeros or more than 15 digits (zip codes, card numbers, references) stay text and are never turned into numbers.
- **Files**: CSV in UTF-8, UTF-16 or Windows-1252, delimiter detected automatically (`, ; tab |`), title rows above the header skipped, ragged rows padded, duplicate or empty headers renamed. Also reads `.xlsx` (via base64) and JSON/JSONL.
- **Safety**: in CSV output, text cells starting with `= + - @` get a `'` prefix so spreadsheets do not run them as formulas. In `.xlsx` output they are stored as strings. Schema regexes with nested quantifiers are refused (ReDoS).

## Install

1. Buy credits at <https://mcp.keyflint.dev> to get a license (`LIC-...`).
2. Add the server to your client:

```json
{
  "mcpServers": {
    "dataclean": {
      "command": "uvx",
      "args": ["dataclean-mcp"],
      "env": { "KEYFLINT_LICENSE": "LIC-..." }
    }
  }
}
```

Claude Desktop also installs the `.mcpb` bundle attached to each GitHub release. Hosted mode runs `dataclean-mcp --http` (or uses the Dockerfile) and reads the license from each request's `Authorization: Bearer LIC-...` header.

Limits (set by environment variables): `DATACLEAN_MAX_BYTES` (20 MB), `DATACLEAN_MAX_ROWS` (200,000), `DATACLEAN_MAX_OUTPUT` (15 MB). Credit prices can be overridden with `DATACLEAN_PRICES=clean_table=2,...`.

## Visual cards (MCP Apps)

In Claude, ChatGPT, VS Code and Goose every paid tool and `credits_balance` renders as an interactive card in the
chat: credit balance with a refill bar and a buy button, or a result summary (key figures, issues, table preview and
credits charged). It is the `ui://dataclean/card.html` resource, self-contained with no network access. Other clients
keep getting the same JSON as before.

## Tests

```bash
pip install -e '.[test]'
pytest -q
```

The tests use a fake KeyFlint gateway and locally signed licenses, so they need no network.

## License

[Business Source License 1.1](LICENSE). Production use requires a KeyFlint license from the Licensor. On 2030-11-05 the code becomes Apache 2.0.
