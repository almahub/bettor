import json

import pytest

from better.config import Settings
from better.services.application import UnconfiguredProvider
from better.services.provider_factory import SourceConfiguration, build_providers


def test_default_sources_are_explicitly_unconfigured():
    missing = UnconfiguredProvider()
    bundle = build_providers(Settings(), missing)
    assert bundle.fixtures is missing
    assert bundle.stats is missing
    assert bundle.validation_history == () and bundle.portfolio is None


def test_configured_feed_requires_url_and_does_not_fake(tmp_path, monkeypatch):
    path = tmp_path / "sources.yaml"
    path.write_text("providers:\n  odds: {primary: http_feed}\n", encoding="utf-8")
    monkeypatch.delenv("BETTER_FEED_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="BETTER_FEED_BASE_URL"):
        build_providers(Settings(sources_config=str(path)), UnconfiguredProvider())


def test_cannot_claim_fixture_adapter_supplies_odds(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("providers:\n  odds: {primary: football_data_org}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fixtures only"):
        build_providers(Settings(sources_config=str(path)), UnconfiguredProvider())


def test_typed_portfolio_from_local_file(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"bankroll": "5000"}), encoding="utf-8")
    monkeypatch.setenv("BETTER_PORTFOLIO_FILE", str(path))
    bundle = build_providers(Settings(), UnconfiguredProvider())
    assert str(bundle.portfolio.bankroll) == "5000"


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        SourceConfiguration.model_validate({"providers": {"odds": {"primary": "imaginary_vendor"}}})
