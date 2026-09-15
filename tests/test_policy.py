"""F007 — Policy surface: bounded, filterable, summarizable."""

import json

import harness


def _policy_ad_row(ad_id, status, topic, entry_type, campaign_id=222):
    return {
        "ad_group_ad": {
            "status": status,
            "ad": {"id": ad_id},
            "policy_summary": {
                "approval_status": "DISAPPROVED" if entry_type == "PROHIBITED" else "APPROVED_LIMITED",
                "review_status": "REVIEWED",
                "policy_topic_entries": [{"topic": topic, "type": entry_type}],
            },
        },
        "ad_group": {"id": 201},
        "campaign": {"id": campaign_id, "name": f"Campaign {campaign_id}"},
    }


def _asset_policy_row():
    return {
        "asset_group_asset": {
            "status": "ENABLED",
            "asset_group": "customers/9876543210/assetGroups/501",
            "asset": "customers/9876543210/assets/777005",
            "field_type": "HEADLINE",
            "policy_summary": {
                "approval_status": "APPROVED_LIMITED",
                "review_status": "REVIEWED",
                "policy_topic_entries": [{"topic": "TRADEMARKS", "type": "LIMITED"}],
            },
        },
        "campaign": {"id": 111, "name": "ACME - PMax - Retail"},
    }


def _stub_policy(client, n_extra=0):
    ads = [
        _policy_ad_row(901, "ENABLED", "DESTINATION_NOT_WORKING", "PROHIBITED"),
        _policy_ad_row(902, "PAUSED", "DESTINATION_NOT_WORKING", "LIMITED"),
    ]
    ads += [
        _policy_ad_row(1000 + i, "ENABLED", "MISREPRESENTATION", "LIMITED")
        for i in range(n_extra)
    ]
    client.stub("ad_group_ad", ads)
    client.stub("asset_group_asset", [_asset_policy_row()])
    return client


def test_summary_mode_topic_histogram_and_status_breakdown(tmp_path, account_client):
    _stub_policy(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_policy_issues", {"mode": "summary"})
    )
    assert payload["mode"] == "summary"
    assert payload["total_issues"] == 3
    topics = {t["topic"]: t["count"] for t in payload["topics"]}
    assert topics == {"DESTINATION_NOT_WORKING": 2, "TRADEMARKS": 1}
    assert payload["entity_status_breakdown"] == {"ENABLED": 2, "PAUSED": 1}


def test_summary_stays_bounded_at_scale(tmp_path, account_client):
    """The summary must stay bounded even for a large issue set."""
    _stub_policy(account_client, n_extra=500)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_policy_issues", {"mode": "summary"})
    )
    assert payload["total_issues"] == 503
    size = len(json.dumps(payload))
    assert size < 20_000, f"summary payload is {size} bytes for 503 issues — not bounded"


def test_filters_enabled_only_campaign_and_topic(tmp_path, account_client):
    _stub_policy(account_client)
    server = harness.build_server(tmp_path, client=account_client)

    enabled = harness.expect_ok(
        harness.call(
            server, "get_policy_issues", {"mode": "full", "enabled_only": True}
        )
    )
    statuses = {e["entity_status"] for e in enabled["issues"]}
    assert statuses == {"ENABLED"}, f"enabled_only leaked: {statuses}"

    by_campaign = harness.expect_ok(
        harness.call(
            server, "get_policy_issues", {"mode": "full", "campaign_id": "111"}
        )
    )
    assert by_campaign["issues"], "campaign filter returned nothing"
    assert {e["campaign_id"] for e in by_campaign["issues"]} == {"111"}

    by_topic = harness.expect_ok(
        harness.call(
            server, "get_policy_issues", {"mode": "full", "topic": "TRADEMARKS"}
        )
    )
    assert by_topic["issues"], "topic filter returned nothing"
    for entry in by_topic["issues"]:
        assert "TRADEMARKS" in json.dumps(entry), entry


def test_full_mode_paginates_with_explicit_token(tmp_path, account_client):
    _stub_policy(account_client, n_extra=120)
    server = harness.build_server(
        tmp_path, client=account_client, env={"ADS_MCP_ROW_LIMIT": "50"}
    )
    page1 = harness.expect_ok(
        harness.call(server, "get_policy_issues", {"mode": "full"})
    )
    assert len(page1["issues"]) <= 50, "full mode ignored the row limit"
    assert page1.get("next_page_token"), "oversize full report must paginate explicitly"
    total = len(page1["issues"])
    token = page1["next_page_token"]
    for _ in range(10):
        page = harness.expect_ok(
            harness.call(server, "get_policy_issues", {"mode": "full", "page_token": token})
        )
        assert len(page["issues"]) <= 50
        total += len(page["issues"])
        token = page.get("next_page_token")
        if not token:
            break
    assert total == 123, f"pagination lost issues: {total}/123"


def test_asset_policy_issues_reachable(tmp_path, account_client):
    """Policy issues on PMax assets must be visible."""
    _stub_policy(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_policy_issues", {"mode": "full"})
    )
    asset_entries = [
        e for e in payload["issues"] if e["entity_type"] == "ASSET_GROUP_ASSET"
    ]
    assert asset_entries, "PMax asset-group asset policy issues are not reachable"
    entry = asset_entries[0]
    assert entry["campaign_id"] == "111"
    assert "TRADEMARKS" in json.dumps(entry)
