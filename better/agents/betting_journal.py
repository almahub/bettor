from typing import Protocol

from better.domain.enums import AgentStatus, DecisionStatus
from better.domain.models import Degradation
from better.domain.schemas import DecisionReport, JournalReport

from .base import BaseAgent


class JournalRepository(Protocol):
    def append_pre_match(self, *, analysis_id, match_id, as_of, kickoff, payload: dict) -> str: ...


class BettingJournal(BaseAgent):
    agent_id = "betting_journal"
    dependencies = ("professional_bettor",)
    allowed_tools = frozenset({"append_only_journal"})

    def __init__(self, settings, journal: JournalRepository | None = None):
        super().__init__(settings)
        self.journal = journal

    async def _run(self, context):
        report = self.payload(context, "professional_bettor", DecisionReport)
        if report is None:
            return self.missing(context, "No final decision available for journal")
        if self.journal is None:
            return self.output(
                context,
                JournalReport(
                    entry_id="",
                    recorded=False,
                    operational_entry=report.decision.status == DecisionStatus.BET,
                ),
                summary="Journal repository not configured",
                status=AgentStatus.PARTIAL,
                unknowns=("Decision not persisted by journal agent",),
                degradations=(
                    Degradation(
                        code="JOURNAL_NOT_PERSISTED",
                        missing_checks=("append_only_journal_write",),
                        critical=False,
                        affected_gates=("CONTEXT_INTEGRITY",),
                        impact="WARNING_ONLY",
                        reason="No repository was supplied to this journal agent. The existing decision "
                        "is not changed, but this output must not be presented as a persisted journal entry.",
                    ),
                ),
                operationally_eligible=report.decision.status == DecisionStatus.BET,
            )
        if context.as_of >= context.match.kickoff:
            return self.missing(context, "Pre-match journal cannot record a post-kickoff decision")
        entry_id = self.journal.append_pre_match(
            analysis_id=context.analysis_id,
            match_id=context.match.match_id,
            as_of=context.as_of,
            kickoff=context.match.kickoff,
            payload={
                "decision": report.decision.model_dump(mode="json"),
                "prompt_version": self.prompt_version,
                "agent_version": self.agent_version,
                "run_id": str(context.run_id),
                "operational_entry": report.decision.status == DecisionStatus.BET,
            },
        )
        return self.output(
            context,
            JournalReport(
                entry_id=str(entry_id),
                recorded=True,
                operational_entry=report.decision.status == DecisionStatus.BET,
            ),
            summary="Immutable pre-match decision appended",
            operationally_eligible=report.decision.status == DecisionStatus.BET,
        )
