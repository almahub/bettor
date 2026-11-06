from datetime import datetime, timezone
from uuid import uuid4

import pytest

from better.agents.registry import AGENT_REGISTRY, create_registry
from better.config import Settings
from better.domain.errors import BetterError
from better.domain.models import Portfolio
from better.domain.schemas import AgentContext, AgentResult
from better.providers.fake import make_demo_bundle


def test_fourteen_bounded_roles_and_versioned_prompts():
    registry = create_registry(make_demo_bundle(), Settings())
    assert len(registry) == len(AGENT_REGISTRY) == 14
    assert not registry.is_enabled("matched_betting")
    for agent in registry:
        assert "1.0.0" in agent.system_prompt
        assert "non fidato" in agent.system_prompt
        assert agent.allowed_tools
        assert "betting_execution" in agent.forbidden_tools
        with pytest.raises(BetterError):
            agent.require_tool("betting_execution")
    assert "odds_provider" in registry.get("football_data_analyst").forbidden_tools


@pytest.mark.asyncio
async def test_context_least_privilege_and_output_roundtrip():
    now = datetime.now(timezone.utc)
    bundle = make_demo_bundle(as_of=now)
    context = AgentContext(
        analysis_id=uuid4(), run_id=uuid4(), match=bundle.fixtures.match, requested_markets=(), as_of=now
    )
    agent = create_registry(bundle, Settings()).get("football_data_analyst")
    result = await agent.run(context)
    assert AgentResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(BetterError):
        await agent.run(context.model_copy(update={"portfolio": Portfolio()}))
    with pytest.raises(BetterError):
        await agent.run(context.model_copy(update={"upstream_results": {"foreign": result}}))


def test_registry_rejects_duplicate_roles():
    registry = create_registry(make_demo_bundle(), Settings())
    with pytest.raises(ValueError):
        registry.register(registry.get("football_data_analyst"))
