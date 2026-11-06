from datetime import datetime, timezone

import pytest

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.providers.fake import make_demo_bundle
from better.providers.multi_source import MultiSourceProvider


@pytest.mark.asyncio
async def test_multisource_fallback_retains_secondary_provenance():
    now = datetime.now(timezone.utc)
    one, two = make_demo_bundle("missing_data", now), make_demo_bundle("bet", now)
    provider = MultiSourceProvider(one.stats, two.stats)
    result = await provider.get_stats("demo-match", now)
    assert result == two.stats.stats
    assert result.provenance[0].raw_reference == "demo:bet:stats"
    assert provider.comparison_records[0]["action"] == "SECONDARY_FALLBACK"


@pytest.mark.asyncio
async def test_multisource_never_fallbacks_around_invalid_primary_schema():
    now = datetime.now(timezone.utc)
    one, two = make_demo_bundle(as_of=now), make_demo_bundle(as_of=now)
    one.stats.failures["stats"] = ErrorCode.SCHEMA_ERROR
    provider = MultiSourceProvider(one.stats, two.stats)
    with pytest.raises(BetterError) as error:
        await provider.get_stats("demo-match", now)
    assert error.value.detail.code == ErrorCode.SCHEMA_ERROR
    assert provider.comparison_records[0]["action"] == "PRIMARY_INVALID"


@pytest.mark.asyncio
async def test_stats_conflict_preserves_both_values_and_blocks_use():
    now = datetime.now(timezone.utc)
    one, two = make_demo_bundle(as_of=now), make_demo_bundle(as_of=now)
    two.stats.stats = two.stats.stats.model_copy(
        update={"home": two.stats.stats.home.model_copy(update={"xg_per_match": 4.5})}
    )
    provider = MultiSourceProvider(one.stats, two.stats, stats_tolerance=0.25)
    with pytest.raises(BetterError, match="SOURCE_CONFLICT") as error:
        await provider.get_stats("demo-match", now)
    assert "home.xg_per_match" in str(error.value)
    assert "4.5" in str(error.value)
    assert provider.comparison_records[0]["secondary"]["home"]["xg_per_match"] == 4.5


@pytest.mark.asyncio
async def test_complete_matching_bookmaker_market_prices_compared():
    now = datetime.now(timezone.utc)
    one, two = make_demo_bundle(as_of=now), make_demo_bundle(as_of=now)
    quotes = list(two.odds.odds.quotes)
    quotes[0] = quotes[0].model_copy(update={"odds": 2.5})
    two.odds.odds = two.odds.odds.model_copy(update={"quotes": tuple(quotes)})
    provider = MultiSourceProvider(one.odds, two.odds, odds_relative_tolerance=0.03)
    with pytest.raises(BetterError, match="SOURCE_CONFLICT"):
        await provider.get_odds("demo-match", now)
    # An incomplete secondary market cannot be treated as a complete independent consensus.
    two.odds.odds = two.odds.odds.model_copy(update={"quotes": (quotes[0],)})
    assert await provider.get_odds("demo-match", now) == one.odds.odds


@pytest.mark.asyncio
async def test_within_tolerance_does_not_invent_merged_observations():
    now = datetime.now(timezone.utc)
    one, two = make_demo_bundle(as_of=now), make_demo_bundle(as_of=now)
    two.stats.stats = two.stats.stats.model_copy(
        update={"home": two.stats.stats.home.model_copy(update={"xg_per_match": 2.25})}
    )
    provider = MultiSourceProvider(one.stats, two.stats)
    result = await provider.get_stats("demo-match", now)
    assert result.home.xg_per_match == 2.2
    assert provider.comparison_records[0]["action"] == "PRIMARY_WITH_COMPARISON"
