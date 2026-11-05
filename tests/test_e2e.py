"""MCP end to end: tools over the protocol, debits, no charge on bad input, paywall."""

from __future__ import annotations

import base64
import importlib
import io
import json

import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError

from conftest import PUB
from test_core import MESSY, SCHEMA


@pytest.fixture(scope="module")
def license_(gateway):
    return gateway["issue"]("buyer@data.test")


def _env(monkeypatch, gateway, key):
    monkeypatch.setenv("KEYFLINT_LICENSE", key)
    monkeypatch.setenv("KEYFLINT_PUBLIC_KEYS", PUB)
    monkeypatch.setenv("KEYFLINT_URL", gateway["url"])
    monkeypatch.setenv("KEYFLINT_CHECKOUT_URL", "https://example.com/buy")
    monkeypatch.setenv("KEYFLINT_CLOCK_STATE", "off")
    monkeypatch.delenv("DATACLEAN_TRANSPORT", raising=False)
    monkeypatch.delenv("DATACLEAN_PRICES", raising=False)


@pytest.fixture()
def server(gateway, license_, monkeypatch):
    _env(monkeypatch, gateway, license_["license_key"])
    import dataclean_mcp.server as srv

    return importlib.reload(srv)


async def call(srv, name, **args):
    async with Client(srv.mcp) as c:
        res = await c.call_tool(name, args)
    text = res.content[0].text if res.content else ""
    data = res.structured_content or (json.loads(text) if text.startswith("{") else None)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return res.is_error, data, text


async def balance(srv):
    _, data, _ = await call(srv, "credits_balance")
    return data["balance"]


async def test_tools_listed_with_docs(server):
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"profile_table", "clean_table", "validate_schema", "normalize_values", "pricing",
                          "credits_balance"}
    assert "number_locale" in tools["clean_table"].description


async def test_profile_charges_one_credit(server):
    before = await balance(server)
    err, data, text = await call(server, "profile_table", content=MESSY, phone_country="ES")
    assert not err, text
    cols = {c["name"]: c for c in data["columns"]}
    assert cols["Importe"]["type"] == "decimal" and cols["Fecha"]["date_order"] == "dmy"
    assert data["duplicate_rows"] == 1 and data["source"]["delimiter"] == ";"
    assert data["suggested_schema"]["columns"]["Fecha"]["type"] == "date"
    assert data["billing"]["credits_charged"] == 1 and await balance(server) == before - 1


async def test_clean_to_csv_json_and_xlsx(server):
    err, data, text = await call(server, "clean_table", content=MESSY, phone_country="ES", dedupe="exact",
                                 header_style="snake")
    assert not err, text
    lines = data["content"].splitlines()
    assert lines[0] == "fecha,cliente,importe,telefono,email,activo"
    assert lines[1] == "2026-04-03,Café Central,1250.50,+34612345678,Ana@Example.COM,true"
    assert data["duplicates_removed"] == 1 and data["rows"] == 3
    assert {f["reason"] for f in data["flags"]} == set()  # email column is text: nothing invalid
    assert base64.b64decode(data["content_base64"]).startswith(b"\xef\xbb\xbf")
    err, data, text = await call(server, "clean_table", content=MESSY, output_format="json", columns=["Importe"])
    assert not err, text
    assert json.loads(data["content"])[0] == {"Importe": 1250.5}
    err, data, text = await call(server, "clean_table", content=MESSY, output_format="xlsx")
    assert not err, text
    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(base64.b64decode(data["content_base64"]))).active
    assert ws["C2"].value == 1250.5 and ws["A2"].value.year == 2026


async def test_ambiguous_flags_over_protocol(server):
    err, data, text = await call(server, "clean_table", content="d,amount\n03/04/2026,\"1,250\"\n")
    assert not err, text
    assert {f["reason"] for f in data["flags"]} == {"ambiguous_date", "ambiguous_number"}
    assert data["flags"][0]["row"] == 2


async def test_validate_schema_tool(server):
    text = "id,email,amount,country\n1,a@x.com,10,ES\n1,bad,-5,\n"
    err, data, out = await call(server, "validate_schema", content=text, schema=SCHEMA)
    assert not err, out
    assert data["valid"] is False and data["errors_by_rule"]["id:unique"] == 1
    assert data["billing"]["credits_charged"] == 1


async def test_normalize_values(server):
    err, data, text = await call(server, "normalize_values", values=["1.250,50 €", "(3,20)", "n/a", "abc"],
                                 kind="number")
    assert not err, text
    assert [r["output"] for r in data["results"]] == ["1250.50", "-3.20", None, "abc"]
    assert data["results"][3]["valid"] is False and data["results"][3]["reason"] == "invalid_number"
    err, data, text = await call(server, "normalize_values", values=["612345678", "+44 20 7946 0958"], kind="phone",
                                 phone_country="ES")
    assert [r["output"] for r in data["results"]] == ["+34612345678", "+442079460958"]


async def test_bad_input_is_free(server):
    before = await balance(server)
    for name, args in [("clean_table", {"content": ""}), ("clean_table", {"content": "a\n1\n", "output_format": "pdf"}),
                       ("profile_table", {"content_base64": "%%"}),
                       ("validate_schema", {"content": "a\n1\n", "schema": {"columns": {"a": {"pattern": "(a+)+"}}}}),
                       ("normalize_values", {"values": []}), ("clean_table", {"content": "a\n1\n", "date_order": "x"})]:
        err, _, text = await call(server, name, **args)
        assert err, (name, text)
    assert await balance(server) == before


async def test_paywall_when_out_of_credits(gateway, monkeypatch):
    lic = gateway["issue"](tier="community")
    _env(monkeypatch, gateway, lic["license_key"])
    import dataclean_mcp.server as srv

    srv = importlib.reload(srv)
    for _ in range(25):
        err, _, text = await call(srv, "normalize_values", values=["1"])
        assert not err, text
    async with Client(srv.mcp) as c:
        with pytest.raises(MCPError) as exc:
            await c.call_tool("normalize_values", {"values": ["1"]})
    assert "example.com/buy" in str(exc.value)


async def test_pricing_is_free(server):
    err, data, _ = await call(server, "pricing")
    assert not err and data["credits_per_call"]["clean_table"] == 1
