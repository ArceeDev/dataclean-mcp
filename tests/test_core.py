"""Parsers, table reading, cleaning and schema validation (no MCP, no network)."""

from __future__ import annotations

import base64
import io
from datetime import date, datetime
from decimal import Decimal

import pytest

from dataclean_mcp import clean as C
from dataclean_mcp import schema as S
from dataclean_mcp import tables as T
from dataclean_mcp import values as V

MESSY = (
    "Informe de ventas;;;;;\n"
    "\n"
    "Fecha;Cliente ;Importe;Teléfono;Email;Activo\n"
    "03/04/2026;  CafÃ© Central\x00 ;1.250,50 €;612 34 56 78;Ana@Example.COM;sí\n"
    "13/04/2026;Bar Pepe;(45,30);+34 699 111 222;pepe@bar.es;no\n"
    "13/04/2026;Bar Pepe;(45,30);+34 699 111 222;pepe@bar.es;no\n"
    "N/A;Ultramarinos;12.000;;not-an-email;si\n"
)


# ---------------------------------------------------------------- values

@pytest.mark.parametrize("raw,value,locale", [
    ("1.250,50 €", "1250.50", "eu"), ("$1,250.50", "1250.50", "us"), ("(45.30)", "-45.30", "us"),
    ("1.250,50-", "-1250.50", "eu"), ("EUR 12", "12", "plain"), ("12,5 %", "12.5", "eu"),
    ("1 234,5", "1234.5", "eu"), ("1'234.50", "1234.50", "us"), ("-€12", "-12", "plain"),
    ("1.234.567", "1234567", "eu"), ("1,250", "1250", "ambiguous"), ("0,5", "0.5", "eu")])
def test_numbers(raw, value, locale):
    n = V.parse_number(raw)
    assert n is not None and n.value == Decimal(value) and n.locale == locale


@pytest.mark.parametrize("raw", ["abc", "12-3", "1,2,3", "1.2.3,4.5", "", "--5", "€", "1e"])
def test_not_numbers(raw):
    assert V.parse_number(raw) is None


def test_ambiguous_number_resolution():
    n = V.parse_number("1,250")
    assert V.number_for(n, "us") == Decimal("1250")
    assert V.number_for(n, "eu") == Decimal("1.25")
    assert V.number_for(n, "") is None
    m = V.parse_number("1.250")
    assert V.number_for(m, "eu") == Decimal("1250") and V.number_for(m, "us") == Decimal("1.25")


@pytest.mark.parametrize("raw,expected,order", [
    ("2026-04-03", date(2026, 4, 3), "ymd"), ("13/04/2026", date(2026, 4, 13), "dmy"),
    ("04/13/2026", date(2026, 4, 13), "mdy"), ("5 de marzo de 2026", date(2026, 3, 5), "text"),
    ("March 5, 2026", date(2026, 3, 5), "text"), ("5-Mar-26", date(2026, 3, 5), "text"),
    ("Thu, 5 Mar 2026", date(2026, 3, 5), "text"), ("3. März 2026", date(2026, 3, 3), "text"),
    ("05.03.2026", date(2026, 3, 5), "ambiguous")])
def test_dates(raw, expected, order):
    w = V.parse_date(raw)
    assert w is not None and w.d == expected and w.order == order


def test_dates_invalid_and_times():
    assert V.parse_date("31/02/2026") is None and V.parse_date("hello") is None
    assert V.parse_date("20260403") is None and V.parse_date("20260403", compact=True).d == date(2026, 4, 3)
    w = V.parse_date("2026-04-03 3:04 pm")
    assert w.t.hour == 15


def test_text_hygiene():
    assert V.clean_text("  CafÃ© SeÃ±or\x00 ​  x ") == "Café Señor x"
    assert V.fix_mojibake("Café") == "Café"  # already fine: untouched
    assert V.is_null(" N/A ") and V.is_null("#N/A") and not V.is_null("0")


def test_phones_emails_iban_urls():
    assert V.parse_phone("612 34 56 78", "ES") == "+34612345678"
    assert V.parse_phone("0034 612345678") == "+34612345678"
    assert V.parse_phone("+44 (0)20 7946 0958") == "+442079460958"
    assert V.parse_phone("(415) 555-2671", "US") == "+14155552671"
    assert V.parse_phone("612 34 56 78") is None  # no country: not guessed
    assert V.parse_phone("12345", "ES") is None
    assert V.parse_email("Ana@Example.COM") == "Ana@example.com" and V.parse_email("a@b") is None
    assert V.parse_iban("es91 2100 0418 4502 0005 1332") == "ES9121000418450200051332"
    assert V.parse_iban("ES9121000418450200051333") is None
    assert V.parse_url("example.com/x") == "https://example.com/x" and V.parse_url("not a url") is None


# ---------------------------------------------------------------- tables

def test_load_skips_title_rows_and_detects_delimiter():
    t = T.load(MESSY)
    assert t.delimiter == ";" and t.headers[:3] == ["Fecha", "Cliente", "Importe"]
    assert len(t.rows) == 4 and t.line_numbers[0] == 4
    assert any("above the header" in i for i in t.issues) and any("NUL" in i for i in t.issues)


def test_load_bytes_encodings():
    for enc in ("cp1252", "utf-16", "utf-8-sig"):
        raw = "Nombre,Año\nPeña,2026\n".encode(enc)
        t = T.load(content_base64=base64.b64encode(raw).decode())
        assert t.headers == ["Nombre", "Año"] and t.rows[0][0] == "Peña", enc


def test_load_json_and_ragged_rows_and_duplicate_headers():
    t = T.load(rows=[{"a": 1, "b": "x"}, {"a": 2, "c": True}])
    assert t.headers == ["a", "b", "c"] and t.rows[1] == [Decimal(2), None, True]
    t = T.load("a,a,\n1,2,3,4\n5\n")
    assert t.headers == ["a", "a_2", "column_3", "extra_1"]
    assert t.rows[1] == ["5", None, None, None]


def test_xlsx_round_trip_typed_and_formula_safe():
    headers = ["id", "when", "amount", "note"]
    rows = [[Decimal(1), date(2026, 4, 3), Decimal("1250.50"), "=1+1"]]
    raw = T.write_xlsx(headers, rows)
    t = T.load(content_base64=base64.b64encode(raw).decode())
    assert t.source == "xlsx" and t.headers == headers
    assert t.rows[0][2] == 1250.5 and isinstance(t.rows[0][1], datetime) and t.rows[0][3] == "=1+1"
    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(raw)).active
    assert ws["D2"].data_type == "s"


def test_old_xls_and_garbage_refused():
    with pytest.raises(T.TableError, match="xls"):
        T.load(content_base64=base64.b64encode(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest").decode())
    with pytest.raises(T.TableError):
        T.load(content_base64="%%%")
    with pytest.raises(T.TableError):
        T.load(content="   ")


# ---------------------------------------------------------------- cleaning

def _clean(text: str, **kw):
    t = T.load(text)
    cols, data, flags = C.clean_table(t.headers, t.rows, C.Options(**kw))
    return t, {c.name: c for c in cols}, data, flags


def test_clean_messy_table():
    t, cols, data, flags = _clean(MESSY, phone_country="ES")
    assert cols["Fecha"].kind == "date" and cols["Fecha"].date_order == "dmy"
    assert data[0][0] == date(2026, 4, 3)  # 03/04 settled day-first by 13/04
    assert cols["Importe"].kind == "decimal" and cols["Importe"].number_locale == "eu"
    assert [r[2] for r in data] == [Decimal("1250.50"), Decimal("-45.30"), Decimal("-45.30"), Decimal("12000")]
    assert cols["Importe"].currency == "€"
    assert data[0][1] == "Café Central" and data[1][1] == "Bar Pepe"
    assert cols["Teléfono"].kind == "phone" and data[0][3] == "+34612345678"
    assert cols["Email"].kind == "text"  # 2 of 3 valid < 90%: not forced
    assert cols["Activo"].kind == "boolean" and data[0][5] is True
    assert data[3][0] is None  # N/A


def test_ambiguous_dates_are_flagged_not_guessed():
    text = "date,v\n03/04/2026,1\n05/06/2026,2\n"
    _, cols, data, flags = _clean(text)
    assert data[0][0] == "03/04/2026" and {f[3] for f in flags} == {"ambiguous_date"}
    assert any("date_order" in n for n in cols["date"].notes)
    _, _, data, flags = _clean(text, date_order="mdy")
    assert data[0][0] == date(2026, 3, 4) and not flags


def test_ambiguous_numbers_need_locale():
    text = "amount\n1,250\n2,500\n"
    _, cols, data, flags = _clean(text)
    assert data[0][0] == "1,250" and flags[0][3] == "ambiguous_number"
    _, _, data, _ = _clean(text, number_locale="eu")
    assert data[0][0] == Decimal("1.25")
    _, _, data, _ = _clean("amount\n1,250\n3,000.75\n")  # the other row settles it: US
    assert data[0][0] == Decimal("1250")


def test_leading_zero_ids_stay_text():
    _, cols, data, _ = _clean("zip,card\n01234,4111111111111111111\n28001,4000000000000000002\n")
    assert cols["zip"].kind == "text" and data[0][0] == "01234"
    assert cols["card"].kind == "text"


def test_invalid_values_kept_or_nulled():
    text = "n\n1\n2\n3\n4\n5\n6\n7\n8\n9\nten\n"
    _, cols, data, flags = _clean(text)
    assert cols["n"].kind == "integer" and data[9][0] == "ten" and flags[0][3] == "invalid_number"
    _, _, data, _ = _clean(text, on_invalid="null")
    assert data[9][0] is None


def test_date_order_conflict_flagged():
    _, cols, data, flags = _clean("d\n13/04/2026\n14/04/2026\n04/15/2026\n")
    assert cols["d"].date_order == "dmy" and data[2][0] == "04/15/2026"
    assert flags[0][3] == "date_order_conflict"


def test_forced_types_and_unknown_type():
    _, cols, data, _ = _clean("zip\n28001\n08001\n", types={"zip": "integer"})
    assert cols["zip"].kind == "integer" and data[1][0] == Decimal(8001)
    with pytest.raises(C.CleanError):
        _clean("a\n1\n", types={"a": "money"})
    with pytest.raises(C.CleanError):
        _clean("a\n1\n", types={"b": "text"})


def test_dedupe():
    t, _, data, _ = _clean(MESSY, phone_country="ES")
    keep, removed = C.dedupe(t.headers, data, [], False)
    assert removed == 1 and keep == [0, 1, 3]
    keep, removed = C.dedupe(t.headers, data, ["Activo"], False)
    assert removed == 2


def test_formula_cells_noted_and_guarded():
    _, cols, data, _ = _clean("note\n=HYPERLINK(\"x\")\nok\n")
    assert any("formula" in n for n in cols["note"].notes)
    out = T.write_csv(["note"], data)
    assert "'=HYPERLINK" in out
    assert "-12" in T.write_csv(["n"], [[Decimal("-12")]])  # numbers are not text: no guard


def test_suggested_schema_validates_its_own_table():
    t, cols, _, _ = _clean("id,email,amount\n1,a@x.com,10.5\n2,b@x.com,3\n")
    sch = C.suggest_schema(list(cols.values()), len(t.rows))
    assert sch["columns"]["id"] == {"type": "integer", "required": True, "unique": True}
    res = S.validate(t.headers, t.rows, t.line_numbers, sch, C.Options())
    assert res["valid"], res["errors"]


# ---------------------------------------------------------------- schema

SCHEMA = {"columns": {
    "id": {"type": "integer", "required": True, "unique": True},
    "email": {"type": "email", "required": True},
    "amount": {"type": "decimal", "min": 0, "max": 1000},
    "signup": {"type": "date", "min": "2020-01-01"},
    "status": {"enum": ["active", "churned"]},
    "sku": {"pattern": "[A-Z]{3}-\\d{4}", "max_length": 8},
    "country": {"required": True},
}}


def test_validate_schema_reports_each_rule_with_row_numbers():
    text = ("id,email,amount,signup,status,sku\n"
            "1,a@x.com,10,2021-05-01,active,ABC-1234\n"
            "1,bad,-5,2019-01-01,Paused,abc\n"
            ",c@x.com,2000,soon,ACTIVE,ABC-12345\n")
    t = T.load(text)
    res = S.validate(t.headers, t.rows, t.line_numbers, SCHEMA, C.Options())
    rules = {(e["rule"], e["column"], e.get("row")) for e in res["errors"]}
    assert ("missing_column", "country", None) in rules
    assert ("unique", "id", 3) in rules and ("required", "id", 4) in rules
    assert ("type", "email", 3) in rules
    assert ("min", "amount", 3) in rules and ("max", "amount", 4) in rules
    assert ("min", "signup", 3) in rules and ("type", "signup", 4) in rules
    assert ("enum", "status", 3) in rules and ("enum", "status", 4) not in rules  # case-insensitive
    assert ("pattern", "sku", 3) in rules and ("max_length", "sku", 4) in rules
    assert not res["valid"]


def test_schema_errors_are_user_errors():
    t = T.load("a\n1\n")
    for bad in ({}, {"columns": {"a": {"type": "money"}}}, {"columns": {"a": {"colour": 1}}},
                {"columns": {"a": {"pattern": "(a+)+$"}}}, {"columns": {"a": {"type": "text", "min": 1}}}):
        with pytest.raises(ValueError):
            S.validate(t.headers, t.rows, t.line_numbers, bad, C.Options())


def test_strict_columns():
    t = T.load("a,b\n1,2\n")
    res = S.validate(t.headers, t.rows, t.line_numbers, {"columns": {"A": {"type": "integer"}},
                                                         "strict_columns": True}, C.Options())
    assert [e["rule"] for e in res["errors"]] == ["unexpected_column"]
