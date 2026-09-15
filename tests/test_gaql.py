"""F005 — GAQL passthrough without silent field loss."""

import pytest

import harness

QUERY = (
    "SELECT campaign.id, campaign.name, campaign.status, "
    "campaign.maximize_conversion_value.target_roas, metrics.clicks, "
    "metrics.conversions FROM campaign ORDER BY campaign.id"
)

SELECTED = [
    "campaign.id",
    "campaign.name",
    "campaign.status",
    "campaign.maximize_conversion_value.target_roas",
    "metrics.clicks",
    "metrics.conversions",
]


def _server(tmp_path, client, env=None):
    return harness.build_server(tmp_path, client=client, env=env)


def test_json_format_returns_every_selected_field_losslessly(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(harness.call(server, "run_gaql", {"query": QUERY}))
    assert payload["fields"] == SELECTED
    rows = payload["rows"]
    assert len(rows) == 3
    pmax = rows[0]
    assert pmax["campaign"]["id"] == 111
    assert pmax["campaign"]["status"] == "ENABLED"
    assert pmax["campaign"]["maximize_conversion_value"]["target_roas"] == 3.5
    assert pmax["metrics"]["conversions"] == 2.0
    # Selected zero and unset fields must remain visible.
    search = rows[1]
    assert search["metrics"]["conversions"] == 0.0, "zero-valued selected metric dropped"
    assert search["campaign"]["maximize_conversion_value"]["target_roas"] is None, (
        "selected-but-unset field must be an explicit null, not silently absent"
    )


def test_default_customer_id_actually_works(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    harness.expect_ok(harness.call(server, "run_gaql", {"query": QUERY}))
    assert account_client.searches[-1].customer_id == harness.CUSTOMER_ID


def test_explicit_customer_id_is_normalized_and_used(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    harness.expect_ok(
        harness.call(
            server, "run_gaql", {"query": QUERY, "customer_id": "987-654-3210"}
        )
    )
    assert account_client.searches[-1].customer_id == harness.CUSTOMER_ID


def test_google_400_error_text_passes_through_verbatim(tmp_path, account_client):
    google_text = "Unrecognized field in the query: 'metrics.bogus'."
    account_client.stub_error(harness.make_google_ads_exception([google_text]))
    server = _server(tmp_path, account_client)
    err = harness.expect_error(
        server,
        "run_gaql",
        {"query": "SELECT metrics.bogus FROM campaign"},
        code="GAQL_ERROR",
    )
    assert google_text in err["message"], (
        f"Google's error text must pass through verbatim: {err['message']}"
    )
    assert len(account_client.searches) == 1, "a 400 must not be retried"


def test_table_format_flattens_to_dotted_columns_without_loss(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "run_gaql", {"query": QUERY, "format": "table"})
    )
    assert payload["format"] == "table"
    assert payload["columns"] == SELECTED, (
        "table columns must be the dotted selected fields, in order"
    )
    assert len(payload["rows"]) == 3
    for row in payload["rows"]:
        assert len(row) == len(SELECTED), f"row width mismatch: {row}"
    roas_idx = SELECTED.index("campaign.maximize_conversion_value.target_roas")
    conv_idx = SELECTED.index("metrics.conversions")
    assert payload["rows"][0][roas_idx] == 3.5
    assert payload["rows"][1][roas_idx] is None, "unset field vanished from table"
    assert payload["rows"][1][conv_idx] == 0.0, "zero metric vanished from table"


def test_csv_format_keeps_all_selected_columns(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "run_gaql", {"query": QUERY, "format": "csv"})
    )
    assert payload["format"] == "csv"
    lines = payload["csv"].strip().splitlines()
    header = lines[0]
    for field in SELECTED:
        assert field in header, f"selected field {field} missing from csv header"
    assert len(lines) == 1 + 3, f"expected header + 3 rows: {lines}"


def test_results_above_row_limit_paginate_with_token(tmp_path, account_client):
    account_client.stub(
        "ad_group_criterion",
        [
            {
                "ad_group_criterion": {
                    "criterion_id": 9000 + i,
                    "keyword": {"text": f"kw {i}", "match_type": "EXACT"},
                }
            }
            for i in range(5)
        ],
    )
    server = _server(tmp_path, account_client, env={"ADS_MCP_ROW_LIMIT": "3"})
    q = "SELECT ad_group_criterion.criterion_id FROM ad_group_criterion"
    page1 = harness.expect_ok(harness.call(server, "run_gaql", {"query": q}))
    assert len(page1["rows"]) == 3, "row limit not applied"
    token = page1.get("next_page_token")
    assert token, "truncated result must carry an explicit next_page_token"
    page2 = harness.expect_ok(
        harness.call(server, "run_gaql", {"query": q, "page_token": token})
    )
    assert len(page2["rows"]) == 2
    assert "next_page_token" not in page2 or not page2["next_page_token"], (
        "final page must not advertise another page"
    )
    ids1 = {r["ad_group_criterion"]["criterion_id"] for r in page1["rows"]}
    ids2 = {r["ad_group_criterion"]["criterion_id"] for r in page2["rows"]}
    assert not (ids1 & ids2), "pages overlap"
    assert len(ids1 | ids2) == 5, "pagination lost rows"


@pytest.mark.parametrize(
    "args,code",
    [
        ({"query": ""}, "INVALID_QUERY"),
        ({"query": "   "}, "INVALID_QUERY"),
        ({"query": "UPDATE campaign SET status = 'PAUSED'"}, "INVALID_QUERY"),
        ({"query": "DELETE FROM campaign"}, "INVALID_QUERY"),
        ({"query": "SELECT campaign.id FROM campaign", "page_size": 0}, "INVALID_PAGE_SIZE"),
        ({"query": "SELECT campaign.id FROM campaign", "page_size": -5}, "INVALID_PAGE_SIZE"),
        (
            {"query": "SELECT campaign.id FROM campaign", "page_size": 10_000_000_000},
            "INVALID_PAGE_SIZE",
        ),
    ],
)
def test_adversarial_inputs_produce_named_errors(tmp_path, account_client, args, code):
    server = _server(tmp_path, account_client)
    harness.expect_error(server, "run_gaql", args, code=code)
    assert not account_client.searches, "invalid input must be rejected before any API call"
