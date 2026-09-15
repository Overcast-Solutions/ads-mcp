"""F013 (behavioral) — mutation surface details: explicit unset with correct
field masks, client-side text validation, irreversibility flags."""

import json

import pytest

import harness
from tool_catalog import MUTATION_TOOLS


def _apply(server, plan_id):
    harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan_id, "dry_run": True})
    )
    return harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan_id, "dry_run": False})
    )


def test_update_campaign_clears_troas_with_leaf_field_mask(tmp_path, account_client):
    """Clear the target explicitly with its leaf mask and no zero value."""
    server = harness.build_rw_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(
            server, "update_campaign", {"campaign_id": "111", "clear_target_roas": True}
        )
    )
    plan = payload["plan"]
    masks = [p for op in plan["operations"] for p in op.get("update_mask", [])]
    assert "maximize_conversion_value.target_roas" in masks, (
        f"plan must carry the leaf mask path, got {masks}"
    )
    assert "maximize_conversion_value" not in masks, (
        "parent path in the mask reproduces FIELD_HAS_SUBFIELDS"
    )
    changes = plan["operations"][0].get("changes", {})
    entry = changes.get("maximize_conversion_value.target_roas")
    assert entry is not None, f"plan changes must show the cleared field: {changes}"
    assert entry["old"] == 3.5
    assert entry["new"] is None, "clearing must be an explicit unset, not 0"

    _apply(server, plan["id"])
    live = account_client.live_mutations()
    assert live, "apply produced no live mutation"
    sent_masks = [p for m in live for p in harness.mutation_mask_paths(m)]
    assert "maximize_conversion_value.target_roas" in sent_masks, (
        f"the mutate request must carry the leaf mask path: {sent_masks}"
    )
    assert "maximize_conversion_value" not in sent_masks
    # The operation must NOT smuggle a zero in as the "clear".
    for m in live:
        for op in getattr(m.request, "operations", []):
            update = getattr(op, "update", None)
            mcv = getattr(update, "maximize_conversion_value", None)
            if mcv is None:
                continue
            try:
                assert not mcv._pb.HasField("target_roas"), (
                    "clear must leave target_roas unset, not set it to 0"
                )
            except ValueError:
                pass


def test_update_campaign_clears_tcpa_with_leaf_field_mask(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(
            server, "update_campaign", {"campaign_id": "333", "clear_target_cpa": True}
        )
    )
    masks = [p for op in payload["plan"]["operations"] for p in op.get("update_mask", [])]
    assert "maximize_conversions.target_cpa_micros" in masks, (
        f"tCPA clear must use the leaf path: {masks}"
    )
    assert "maximize_conversions" not in masks


@pytest.mark.parametrize(
    "tool,args,code",
    [
        (
            "draft_responsive_search_ad",
            {
                "ad_group_id": "201",
                "headlines": ["x" * 31, "Good headline", "Also good"],
                "descriptions": ["Fine description", "Another one"],
                "final_url": "https://example.com",
            },
            "TEXT_LIMIT_EXCEEDED",
        ),
        (
            "draft_responsive_search_ad",
            {
                "ad_group_id": "201",
                "headlines": ["Good headline", "Also good", "Third"],
                "descriptions": ["y" * 91, "Another one"],
                "final_url": "https://example.com",
            },
            "TEXT_LIMIT_EXCEEDED",
        ),
        (
            "draft_responsive_search_ad",
            {
                "ad_group_id": "201",
                "headlines": ["Only", "Two"],
                "descriptions": ["Fine description", "Another one"],
                "final_url": "https://example.com",
            },
            "TEXT_COUNT_INVALID",
        ),
        (
            "draft_sitelinks",
            {
                "campaign_id": "222",
                "sitelinks": [
                    {
                        "link_text": "z" * 26,
                        "final_url": "https://example.com",
                        "description1": "One",
                        "description2": "Two",
                    }
                ],
            },
            "TEXT_LIMIT_EXCEEDED",
        ),
        (
            "create_callouts",
            {"campaign_id": "222", "callouts": ["w" * 26]},
            "TEXT_LIMIT_EXCEEDED",
        ),
    ],
)
def test_text_limits_validated_client_side(tmp_path, account_client, tool, args, code):
    server = harness.build_rw_server(tmp_path, client=account_client)
    err = harness.expect_error(server, tool, args, code=code)
    assert any(ch.isdigit() for ch in err["message"]), (
        f"the violated limit must be named: {err['message']}"
    )
    assert account_client.mutations == [], (
        "text validation must happen before any API call"
    )


def test_removals_flagged_irreversible(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    removal = harness.expect_ok(
        harness.call(server, "remove_entity", {"entity_type": "ad", "entity_id": "201~901"})
    )["plan"]
    assert removal["irreversible"] is True
    assert "irreversible" in removal["summary"].lower(), (
        f"the human summary must say irreversible: {removal['summary']}"
    )
    pause = harness.expect_ok(
        harness.call(server, "pause_entity", {"entity_type": "ad", "entity_id": "201~901"})
    )["plan"]
    assert pause["irreversible"] is False


def test_accepted_mutation_names_all_registered(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    names = harness.tool_names(server)
    missing = MUTATION_TOOLS - names
    assert not missing, f"accepted mutation tools missing: {sorted(missing)}"
