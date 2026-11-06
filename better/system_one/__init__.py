"""Optional, vendor-neutral System One observation layer."""

from .config import SystemOneSettings
from .contracts import (
    Question,
    QuestionBatch,
    SemanticAnswer,
    SemanticStatus,
    ShadowRecord,
    SystemOneError,
    SystemOneProvider,
    SystemOneResult,
)
from .providers import FakeSystemOneProvider, RizzoFlowSystemOneProvider
from .shadow import ShadowCoordinator
from .state import SystemOneStateBuilder, state_hash

__all__ = [
    "FakeSystemOneProvider",
    "Question",
    "QuestionBatch",
    "RizzoFlowSystemOneProvider",
    "SemanticAnswer",
    "SemanticStatus",
    "ShadowCoordinator",
    "ShadowRecord",
    "SystemOneError",
    "SystemOneProvider",
    "SystemOneResult",
    "SystemOneSettings",
    "SystemOneStateBuilder",
    "state_hash",
]
