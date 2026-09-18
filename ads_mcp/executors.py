"""Real mutate-request builders — what `confirm_and_apply` actually sends.

Every function here returns ``_run(ctx)``: a closure the plan store holds and
`confirm_and_apply` invokes exactly once, after the dry-run sequence and a
fresh cap re-check. Each builds genuine Google Ads operations against real
proto types, so the request that leaves this process matches the plan the
operator approved — including multi-step flows (budget then campaign, asset
then campaign-asset link) and every id in a batch, not just the first.
"""

from __future__ import annotations

from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp.errors import ToolError

MICROS = 1_000_000
CAMPAIGN_STRATEGY_FIELDS = {
    "MAXIMIZE_CONVERSIONS": "maximize_conversions",
    "MAXIMIZE_CONVERSION_VALUE": "maximize_conversion_value",
    "MANUAL_CPC": "manual_cpc",
    "TARGET_CPA": "target_cpa",
    "TARGET_ROAS": "target_roas",
    "MAXIMIZE_CLICKS": "target_spend",
}
CAMPAIGN_TARGET_FIELDS = (
    "maximize_conversions.target_cpa_micros",
    "maximize_conversion_value.target_roas",
    "target_cpa.target_cpa_micros",
    "target_roas.target_roas",
)


def strategy_mask_paths(field):
    """An empty strategy message still needs its settable leaf masks."""
    from google.ads.googleads.v25.resources.types.campaign import Campaign

    descriptor = Campaign.pb().DESCRIPTOR.fields_by_name[field].message_type
    return [f"{field}.{leaf.name}" for leaf in descriptor.fields]

_MINUTE_ENUM = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}


def _mutate(ctx, service_name: str, method: str, request):
    """Send one mutate request, exactly once, and record that it landed."""
    service = ctx.client().get_service(service_name)
    result = ctx.mutate_once(lambda: getattr(service, method)(request=request))
    ctx.audit_step(
        service=service_name,
        method=method,
        operations=len(request.mutate_operations if "mutate_operations" in
                       request._pb.DESCRIPTOR.fields_by_name else request.operations),
    )
    return result


def _request(ctx, client, req_name: str):
    request = client.get_type(req_name)
    request.customer_id = ctx.config.customer_id
    return request


def _res(ctx, kind: str, *ids) -> str:
    return f"customers/{ctx.config.customer_id}/{kind}/{'~'.join(str(i) for i in ids)}"


def _send(ctx, client, service, method, req_name, ops):
    if not ops:
        raise ToolError("EMPTY_OPERATION", f"{method}: no operations were built")
    request = _request(ctx, client, req_name)
    for op in ops:
        request.operations.append(op)
    return _mutate(ctx, service, method, request)


def _set_present(message, field: str):
    """Mark a singular message field present without setting a scalar."""
    getattr(message._pb, field).SetInParent()


# ---------------------------------------------------------------------------
# Campaigns and budgets


def create_campaign(*, name, daily_budget, bidding_strategy, channel_type,
                    geo_target_ids=(), language_ids=(), final_urls=(),
                    ad_group_name=None, keywords=(), target_cpa=None,
                    target_roas=None, status="PAUSED",
                    contains_eu_political_advertising, network_settings=None):
    def _run(ctx):
        client = ctx.client()

        budget_op = client.get_type("CampaignBudgetOperation")
        budget_op.create.name = f"{name} budget"
        budget_op.create.amount_micros = int(float(daily_budget) * MICROS)
        budget_op.create.explicitly_shared = False
        budget_op.create.delivery_method = (
            client.enums.BudgetDeliveryMethodEnum.STANDARD
        )
        budget_res = _send(
            ctx, client, "CampaignBudgetService", "mutate_campaign_budgets",
            "MutateCampaignBudgetsRequest", [budget_op],
        )
        budget_resource = budget_res.results[0].resource_name

        campaign_op = client.get_type("CampaignOperation")
        campaign_op.create.name = name
        campaign_op.create.status = getattr(client.enums.CampaignStatusEnum, status)
        campaign_op.create.campaign_budget = budget_resource
        campaign_op.create.advertising_channel_type = getattr(
            client.enums.AdvertisingChannelTypeEnum, channel_type
        )
        _campaign_declaration(client, campaign_op.create, contains_eu_political_advertising)
        if network_settings is not None:
            for field, value in network_settings.items():
                setattr(campaign_op.create.network_settings, field, value)
        if channel_type == "PERFORMANCE_MAX":
            campaign_op.create.brand_guidelines_enabled = False
        strategy = str(bidding_strategy).strip().upper()
        field = CAMPAIGN_STRATEGY_FIELDS.get(strategy)
        if field is None:
            raise ToolError(
                "INVALID_BIDDING_STRATEGY",
                f"bidding_strategy {bidding_strategy!r} is not supported for "
                "campaign creation (MAXIMIZE_CONVERSIONS, "
                "MAXIMIZE_CONVERSION_VALUE, MANUAL_CPC)",
            )
        _set_present(campaign_op.create, field)
        strategy_message = getattr(campaign_op.create, field)
        if target_cpa is not None:
            strategy_message.target_cpa_micros = int(float(target_cpa) * MICROS)
        if target_roas is not None:
            strategy_message.target_roas = float(target_roas)
        for url in final_urls or ():
            campaign_op.create.final_url_suffix = ""  # PMax urls live on assets
            break
        campaign_res = _send(
            ctx, client, "CampaignService", "mutate_campaigns",
            "MutateCampaignsRequest", [campaign_op],
        )
        campaign_resource = campaign_res.results[0].resource_name

        criteria = []
        for geo_id in geo_target_ids or ():
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = campaign_resource
            op.create.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
            criteria.append(op)
        for lang_id in language_ids or ():
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = campaign_resource
            op.create.language.language_constant = f"languageConstants/{lang_id}"
            criteria.append(op)
        if criteria:
            _send(
                ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
                "MutateCampaignCriteriaRequest", criteria,
            )
        if ad_group_name:
            group_res = create_ad_group(
                campaign_id=campaign_resource.rsplit("/", 1)[-1],
                name=ad_group_name,
                status=status,
            )(ctx)
            if keywords:
                create_keywords(
                    ad_group_id=group_res.results[0].resource_name.rsplit("/", 1)[-1],
                    keywords=keywords,
                )(ctx)
        return campaign_res

    return _run


def _campaign_declaration(client, campaign, declaration):
    campaign.contains_eu_political_advertising = getattr(
        client.enums.EuPoliticalAdvertisingStatusEnum,
        "CONTAINS_EU_POLITICAL_ADVERTISING" if declaration
        else "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    )


def create_pmax(*, name, daily_budget, bidding_strategy, final_urls, headlines,
                long_headlines, descriptions, business_name,
                contains_eu_political_advertising, landscape_image_asset_ids,
                square_image_asset_ids, logo_asset_ids, geo_target_ids=(),
                start_paused=True):
    """Create the entire initial graph atomically with backward temporary refs."""
    def _run(ctx):
        client = ctx.client()
        request = _request(ctx, client, "MutateGoogleAdsRequest")
        request.partial_failure = False
        request.validate_only = False
        operations = []
        temporary_id = 0

        def create(kind, resource_kind=None):
            nonlocal temporary_id
            wrapper = client.get_type("MutateOperation")
            message = getattr(wrapper, kind + "_operation").create
            if resource_kind:
                temporary_id -= 1
                message.resource_name = _res(ctx, resource_kind, temporary_id)
            operations.append(wrapper)
            return message

        budget = create("campaign_budget", "campaignBudgets")
        budget.name = f"{name} budget"
        budget.amount_micros = int(float(daily_budget) * MICROS)
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False

        campaign = create("campaign", "campaigns")
        campaign.name = name
        campaign.campaign_budget = budget.resource_name
        campaign.status = (client.enums.CampaignStatusEnum.PAUSED if start_paused
                           else client.enums.CampaignStatusEnum.ENABLED)
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
        campaign.brand_guidelines_enabled = False
        _campaign_declaration(client, campaign, contains_eu_political_advertising)
        _set_present(campaign, CAMPAIGN_STRATEGY_FIELDS[bidding_strategy])
        text_fields = (
            [(t, "HEADLINE") for t in headlines]
            + [(t, "LONG_HEADLINE") for t in long_headlines]
            + [(t, "DESCRIPTION") for t in descriptions]
            + [(business_name, "BUSINESS_NAME")]
        )
        assets = []
        for text, field in text_fields:
            asset = create("asset", "assets")
            asset.text_asset.text = text
            assets.append((asset.resource_name, field))

        group = create("asset_group", "assetGroups")
        group.name = f"{name} asset group"
        group.campaign = campaign.resource_name
        group.status = (client.enums.AssetGroupStatusEnum.PAUSED if start_paused
                        else client.enums.AssetGroupStatusEnum.ENABLED)
        group.final_urls.extend(final_urls)
        for identities, field in (
            (landscape_image_asset_ids, "MARKETING_IMAGE"),
            (square_image_asset_ids, "SQUARE_MARKETING_IMAGE"),
            (logo_asset_ids, "LOGO"),
        ):
            assets.extend((_res(ctx, "assets", identity), field) for identity in identities)
        for resource, field in assets:
            link = create("asset_group_asset")
            link.asset_group = group.resource_name
            link.asset = resource
            link.field_type = getattr(client.enums.AssetFieldTypeEnum, field)
        for identity in geo_target_ids:
            criterion = create("campaign_criterion")
            criterion.campaign = campaign.resource_name
            criterion.location.geo_target_constant = f"geoTargetConstants/{identity}"
        request.mutate_operations.extend(operations)
        response = _mutate(ctx, "GoogleAdsService", "mutate", request)
        for result in response.mutate_operation_responses:
            if result._pb.WhichOneof("response") == "campaign_result":
                return {"campaign_resource_name": result.campaign_result.resource_name}
        raise ToolError("MUTATION_RESULT_MISSING", "Google accepted the request but returned no campaign result; reconcile the account before creating another campaign")

    return _run


def update_campaign(*, campaign_id, set_fields, mask_paths):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("CampaignOperation")
        op.update.resource_name = _res(ctx, "campaigns", campaign_id)
        for path, value in set_fields:
            node = op.update
            parts = path.split(".")
            for part in parts[:-1]:
                node = getattr(node, part)
            if value == {}:
                _set_present(node, parts[-1])
            else:
                setattr(node, parts[-1], value)
        client.copy_from(op.update_mask, FieldMask(paths=list(mask_paths)))
        return _send(
            ctx, client, "CampaignService", "mutate_campaigns",
            "MutateCampaignsRequest", [op],
        )

    return _run


def add_campaign_targets(*, campaign_id, geo_target_ids=(), language_ids=()):
    """Add positive criteria without replacing any existing targeting."""
    def _run(ctx):
        client = ctx.client()
        ops = []
        for geo_id in geo_target_ids:
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = _res(ctx, "campaigns", campaign_id)
            op.create.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
            ops.append(op)
        for language_id in language_ids:
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = _res(ctx, "campaigns", campaign_id)
            op.create.language.language_constant = f"languageConstants/{language_id}"
            ops.append(op)
        return _send(ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
                     "MutateCampaignCriteriaRequest", ops)

    return _run


def update_budget(*, budget_id, amount_micros):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = _res(ctx, "campaignBudgets", budget_id)
        op.update.amount_micros = int(amount_micros)
        client.copy_from(op.update_mask, FieldMask(paths=["amount_micros"]))
        return _send(
            ctx, client, "CampaignBudgetService", "mutate_campaign_budgets",
            "MutateCampaignBudgetsRequest", [op],
        )

    return _run


# ---------------------------------------------------------------------------
# Entity status / removal


_ENTITY = {
    "campaign": ("CampaignService", "mutate_campaigns", "CampaignOperation",
                 "MutateCampaignsRequest", "campaigns"),
    "ad_group": ("AdGroupService", "mutate_ad_groups", "AdGroupOperation",
                 "MutateAdGroupsRequest", "adGroups"),
    "ad": ("AdGroupAdService", "mutate_ad_group_ads", "AdGroupAdOperation",
           "MutateAdGroupAdsRequest", "adGroupAds"),
    "keyword": ("AdGroupCriterionService", "mutate_ad_group_criteria",
                "AdGroupCriterionOperation", "MutateAdGroupCriteriaRequest",
                "adGroupCriteria"),
}


def set_entity_status(*, entity_type, entity_id, status_name):
    def _run(ctx):
        client = ctx.client()
        service, method, op_name, req_name, path = _ENTITY[entity_type]
        op = client.get_type(op_name)
        op.update.resource_name = _res(ctx, path, entity_id)
        op.update.status = getattr(type(op.update.status), status_name)
        client.copy_from(op.update_mask, FieldMask(paths=["status"]))
        return _send(ctx, client, service, method, req_name, [op])

    return _run


def remove_entities(*, entity_type, resource_names):
    """Remove EVERY named resource — not just the first."""

    def _run(ctx):
        client = ctx.client()
        service, method, op_name, req_name, _path = _ENTITY[entity_type]
        ops = []
        for resource in resource_names:
            op = client.get_type(op_name)
            op.remove = resource
            ops.append(op)
        return _send(ctx, client, service, method, req_name, ops)

    return _run


def remove_campaign_criteria(*, resource_names):
    def _run(ctx):
        client = ctx.client()
        ops = []
        for resource in resource_names:
            op = client.get_type("CampaignCriterionOperation")
            op.remove = resource
            ops.append(op)
        return _send(
            ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
            "MutateCampaignCriteriaRequest", ops,
        )

    return _run


# ---------------------------------------------------------------------------
# Ad groups, ads, keywords


def create_ad_group(*, campaign_id, name, cpc_bid_micros=None, status="PAUSED"):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("AdGroupOperation")
        op.create.name = name
        op.create.campaign = _res(ctx, "campaigns", campaign_id)
        op.create.status = getattr(client.enums.AdGroupStatusEnum, status)
        if cpc_bid_micros is not None:
            op.create.cpc_bid_micros = int(cpc_bid_micros)
        return _send(
            ctx, client, "AdGroupService", "mutate_ad_groups",
            "MutateAdGroupsRequest", [op],
        )

    return _run


def update_ad_group(*, ad_group_id, set_fields, mask_paths):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("AdGroupOperation")
        op.update.resource_name = _res(ctx, "adGroups", ad_group_id)
        for path, value in set_fields:
            if path == "status":
                op.update.status = getattr(client.enums.AdGroupStatusEnum, value)
            elif path == "ad_rotation_mode":
                op.update.ad_rotation_mode = getattr(client.enums.AdGroupAdRotationModeEnum, value)
            else:
                setattr(op.update, path, value)
        client.copy_from(op.update_mask, FieldMask(paths=list(mask_paths)))
        return _send(
            ctx, client, "AdGroupService", "mutate_ad_groups",
            "MutateAdGroupsRequest", [op],
        )

    return _run


def create_rsa(*, ad_group_id, headlines, descriptions, final_url,
               path1=None, path2=None, status="PAUSED"):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("AdGroupAdOperation")
        op.create.ad_group = _res(ctx, "adGroups", ad_group_id)
        op.create.status = getattr(client.enums.AdGroupAdStatusEnum, status)
        if path1 is not None:
            op.create.ad.responsive_search_ad.path1 = path1
        if path2 is not None:
            op.create.ad.responsive_search_ad.path2 = path2
        op.create.ad.final_urls.append(final_url)
        for text in headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            op.create.ad.responsive_search_ad.headlines.append(asset)
        for text in descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            op.create.ad.responsive_search_ad.descriptions.append(asset)
        return _send(
            ctx, client, "AdGroupAdService", "mutate_ad_group_ads",
            "MutateAdGroupAdsRequest", [op],
        )

    return _run


def create_keywords(*, ad_group_id, keywords):
    def _run(ctx):
        client = ctx.client()
        ops = []
        for kw in keywords:
            op = client.get_type("AdGroupCriterionOperation")
            op.create.ad_group = _res(ctx, "adGroups", ad_group_id)
            op.create.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            op.create.keyword.text = kw["text"]
            op.create.keyword.match_type = getattr(
                client.enums.KeywordMatchTypeEnum,
                kw["match_type"],
            )
            if kw.get("cpc_bid_micros") is not None:
                op.create.cpc_bid_micros = int(kw["cpc_bid_micros"])
            ops.append(op)
        return _send(
            ctx, client, "AdGroupCriterionService", "mutate_ad_group_criteria",
            "MutateAdGroupCriteriaRequest", ops,
        )

    return _run


def update_keyword_bid(*, ad_group_id, criterion_id, new_bid_micros):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = _res(ctx, "adGroupCriteria", ad_group_id, criterion_id)
        op.update.cpc_bid_micros = int(new_bid_micros)
        client.copy_from(op.update_mask, FieldMask(paths=["cpc_bid_micros"]))
        return _send(
            ctx, client, "AdGroupCriterionService", "mutate_ad_group_criteria",
            "MutateAdGroupCriteriaRequest", [op],
        )

    return _run


def add_negative_keywords(*, campaign_id, keywords):
    def _run(ctx):
        client = ctx.client()
        ops = []
        for kw in keywords:
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = _res(ctx, "campaigns", campaign_id)
            op.create.negative = True
            op.create.keyword.text = kw["text"]
            op.create.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, kw["match_type"])
            ops.append(op)
        return _send(
            ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
            "MutateCampaignCriteriaRequest", ops,
        )

    return _run


# ---------------------------------------------------------------------------
# Assets and extensions


def _create_assets_and_link(ctx, client, *, campaign_id, asset_builders, field_type):
    asset_ops = []
    for build in asset_builders:
        op = client.get_type("AssetOperation")
        build(op.create)
        asset_ops.append(op)
    asset_res = _send(
        ctx, client, "AssetService", "mutate_assets", "MutateAssetsRequest", asset_ops
    )
    link_ops = []
    for result in asset_res.results:
        op = client.get_type("CampaignAssetOperation")
        op.create.campaign = _res(ctx, "campaigns", campaign_id)
        op.create.asset = result.resource_name
        op.create.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)
        link_ops.append(op)
    _send(
        ctx, client, "CampaignAssetService", "mutate_campaign_assets",
        "MutateCampaignAssetsRequest", link_ops,
    )
    return asset_res


def create_sitelinks(*, campaign_id, sitelinks):
    def _run(ctx):
        client = ctx.client()

        def builder(link):
            def build(asset):
                asset.sitelink_asset.link_text = link["link_text"]
                if link.get("description1"):
                    asset.sitelink_asset.description1 = link["description1"]
                if link.get("description2"):
                    asset.sitelink_asset.description2 = link["description2"]
                asset.final_urls.append(link["final_url"])

            return build

        return _create_assets_and_link(
            ctx, client, campaign_id=campaign_id,
            asset_builders=[builder(link) for link in sitelinks],
            field_type="SITELINK",
        )

    return _run


def create_callouts(*, campaign_id, callouts):
    def _run(ctx):
        client = ctx.client()

        def builder(text):
            def build(asset):
                asset.callout_asset.callout_text = text

            return build

        return _create_assets_and_link(
            ctx, client, campaign_id=campaign_id,
            asset_builders=[builder(t) for t in callouts],
            field_type="CALLOUT",
        )

    return _run


def create_structured_snippets(*, campaign_id, header, values):
    def _run(ctx):
        client = ctx.client()

        def build(asset):
            asset.structured_snippet_asset.header = header
            for value in values:
                asset.structured_snippet_asset.values.append(value)

        return _create_assets_and_link(
            ctx, client, campaign_id=campaign_id, asset_builders=[build],
            field_type="STRUCTURED_SNIPPET",
        )

    return _run


def create_image_asset(*, asset_name, image_bytes):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("AssetOperation")
        op.create.name = asset_name
        # Asset.type_ is output-only: Google infers it from the asset payload.
        op.create.image_asset.data = image_bytes
        return _send(
            ctx, client, "AssetService", "mutate_assets",
            "MutateAssetsRequest", [op],
        )

    return _run


def create_text_asset(*, asset_name, text):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("AssetOperation")
        op.create.name = asset_name
        # Asset.type_ is output-only: Google infers it from the asset payload.
        op.create.text_asset.text = text
        return _send(
            ctx, client, "AssetService", "mutate_assets",
            "MutateAssetsRequest", [op],
        )

    return _run


def remove_campaign_asset(*, campaign_id, asset_id, field_type):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("CampaignAssetOperation")
        op.remove = _res(ctx, "campaignAssets", campaign_id, asset_id, field_type)
        return _send(
            ctx, client, "CampaignAssetService", "mutate_campaign_assets",
            "MutateCampaignAssetsRequest", [op],
        )

    return _run


# ---------------------------------------------------------------------------
# Audiences, geo, conversions, bidding, schedules


def create_custom_audience(*, name, audience_type, members):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("CustomAudienceOperation")
        op.create.name = name
        # type_ is settable and meaningful; status is documented output-only,
        # so setting it would earn a RESOURCE_READ_ONLY rejection.
        op.create.type_ = getattr(
            client.enums.CustomAudienceTypeEnum, audience_type
        )
        for value in members:
            member = client.get_type("CustomAudienceMember")
            text = str(value)
            looks_like_url = "." in text and " " not in text
            if looks_like_url:
                member.member_type = client.enums.CustomAudienceMemberTypeEnum.URL
                member.url = text
            else:
                member.member_type = client.enums.CustomAudienceMemberTypeEnum.KEYWORD
                member.keyword = text
            op.create.members.append(member)
        return _send(
            ctx, client, "CustomAudienceService", "mutate_custom_audiences",
            "MutateCustomAudiencesRequest", [op],
        )

    return _run


def add_audience_criterion(*, campaign_id, audience_id, targeting_mode):
    """Attach an audience, encoding observation-vs-targeting for real.

    The distinction lives in Campaign.targeting_setting.target_restrictions,
    not on the criterion: without it, an "observation" plan silently narrows
    delivery to the audience, which is a spend-shape change the operator did
    not approve.
    """

    def _run(ctx):
        client = ctx.client()
        campaign_resource = _res(ctx, "campaigns", campaign_id)

        # target_restrictions is a masked write of a whole repeated field:
        # sending only ours would silently delete the campaign's AGE_RANGE,
        # GENDER and INCOME_RANGE restrictions. Read, merge, then write.
        existing = ctx.search(
            "SELECT campaign.id, campaign.targeting_setting.target_restrictions "
            f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
        )
        matched = [r for r in existing if str(r.campaign.id) == str(campaign_id)]
        if not matched:
            # Writing target_restrictions is a masked write of the WHOLE
            # repeated field. Without a confirmed read we would delete every
            # other targeting dimension, so refuse instead of guessing.
            raise ToolError(
                "TARGETING_READ_FAILED",
                f"could not read campaign {campaign_id}'s existing targeting "
                "restrictions; refusing to rewrite them blind",
            )
        preserved = [
            r
            for row in matched
            for r in row.campaign.targeting_setting.target_restrictions
            if r.targeting_dimension.name != "AUDIENCE"
        ]

        restriction_op = client.get_type("CampaignOperation")
        restriction_op.update.resource_name = campaign_resource
        for r in preserved:
            restriction_op.update.targeting_setting.target_restrictions.append(r)
        restriction = client.get_type("TargetRestriction")
        restriction.targeting_dimension = (
            client.enums.TargetingDimensionEnum.AUDIENCE
        )
        restriction.bid_only = targeting_mode == "OBSERVATION"
        restriction_op.update.targeting_setting.target_restrictions.append(restriction)
        client.copy_from(
            restriction_op.update_mask,
            FieldMask(paths=["targeting_setting.target_restrictions"]),
        )
        _send(
            ctx, client, "CampaignService", "mutate_campaigns",
            "MutateCampaignsRequest", [restriction_op],
        )

        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = campaign_resource
        op.create.user_list.user_list = _res(ctx, "userLists", audience_id)
        return _send(
            ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
            "MutateCampaignCriteriaRequest", [op],
        )

    return _run


def exclude_geo_criterion(*, campaign_id, geo_target_id):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = _res(ctx, "campaigns", campaign_id)
        op.create.negative = True
        op.create.location.geo_target_constant = f"geoTargetConstants/{geo_target_id}"
        return _send(
            ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
            "MutateCampaignCriteriaRequest", [op],
        )

    return _run


def create_conversion_action(*, name, category, counting_type="ONE_PER_CLICK",
                             click_through_lookback_window_days=30):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("ConversionActionOperation")
        op.create.name = name
        op.create.status = client.enums.ConversionActionStatusEnum.ENABLED
        op.create.type_ = client.enums.ConversionActionTypeEnum.WEBPAGE
        op.create.category = getattr(
            client.enums.ConversionActionCategoryEnum, str(category).upper()
        )
        op.create.counting_type = getattr(client.enums.ConversionActionCountingTypeEnum, counting_type)
        op.create.click_through_lookback_window_days = click_through_lookback_window_days
        return _send(
            ctx, client, "ConversionActionService", "mutate_conversion_actions",
            "MutateConversionActionsRequest", [op],
        )

    return _run


def set_conversion_action_primary(*, conversion_action_id, primary):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("ConversionActionOperation")
        op.update.resource_name = _res(ctx, "conversionActions", conversion_action_id)
        op.update.primary_for_goal = bool(primary)
        client.copy_from(op.update_mask, FieldMask(paths=["primary_for_goal"]))
        return _send(
            ctx, client, "ConversionActionService", "mutate_conversion_actions",
            "MutateConversionActionsRequest", [op],
        )

    return _run


def create_bidding_strategy(*, name, strategy_type, target_cpa, target_roas):
    def _run(ctx):
        client = ctx.client()
        op = client.get_type("BiddingStrategyOperation")
        op.create.name = name
        if strategy_type == "TARGET_CPA":
            op.create.target_cpa.target_cpa_micros = int(float(target_cpa) * MICROS)
        else:
            op.create.target_roas.target_roas = float(target_roas)
        return _send(
            ctx, client, "BiddingStrategyService", "mutate_bidding_strategies",
            "MutateBiddingStrategiesRequest", [op],
        )

    return _run


def create_ad_schedules(*, campaign_id, schedules):
    def _run(ctx):
        client = ctx.client()
        ops = []
        for sched in schedules:
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = _res(ctx, "campaigns", campaign_id)
            info = op.create.ad_schedule
            info.day_of_week = getattr(
                client.enums.DayOfWeekEnum, str(sched["day_of_week"]).upper()
            )
            info.start_hour = int(sched.get("start_hour", 0))
            info.end_hour = int(sched.get("end_hour", 24))
            start_min = int(sched.get("start_minute", 0))
            end_min = int(sched.get("end_minute", 0))
            for minute, field in ((start_min, "start_minute"), (end_min, "end_minute")):
                if minute not in _MINUTE_ENUM:
                    raise ToolError(
                        "INVALID_SCHEDULE",
                        f"{field} must be one of {sorted(_MINUTE_ENUM)}, got {minute}",
                    )
                setattr(
                    info, field,
                    getattr(client.enums.MinuteOfHourEnum, _MINUTE_ENUM[minute]),
                )
            ops.append(op)
        return _send(
            ctx, client, "CampaignCriterionService", "mutate_campaign_criteria",
            "MutateCampaignCriteriaRequest", ops,
        )

    return _run


# ---------------------------------------------------------------------------
# Recommendations


def apply_recommendation(*, resource_name):
    def _run(ctx):
        client = ctx.client()
        request = _request(ctx, client, "ApplyRecommendationRequest")
        op = request._pb.operations.add()
        op.resource_name = resource_name
        return _mutate(ctx, "RecommendationService", "apply_recommendation", request)

    return _run


def dismiss_recommendation(*, resource_name):
    def _run(ctx):
        client = ctx.client()
        request = _request(ctx, client, "DismissRecommendationRequest")
        op = request._pb.operations.add()
        op.resource_name = resource_name
        return _mutate(ctx, "RecommendationService", "dismiss_recommendation", request)

    return _run
