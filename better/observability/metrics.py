from collections import Counter


class Metrics:
    def __init__(self):
        self.decisions = Counter()
        self.agent_calls = Counter()
        self.agent_errors = Counter()
        self.agent_latency_ms = {}
        self.research = Counter()
        self.research_fields = Counter()
        self.research_source_tiers = Counter()
        self.research_tokens = Counter()
        self.research_cost = 0.0
        self.research_latency_ms = []
        self.football_data_research = Counter()
        self.football_data_fields = Counter()
        self.football_data_tokens = Counter()
        self.football_data_cost = 0.0
        self.football_data_latency_ms = []

    def agent_completed(self, agent, status, duration_ms):
        self.agent_calls[agent] += 1
        self.agent_errors[agent] += int(status not in {"OK", "PARTIAL"})
        self.agent_latency_ms[agent] = duration_ms

    def research_requested(self, fields, *, category="external"):
        counter, field_counter = (
            (self.research, self.research_fields)
            if category == "external"
            else (self.football_data_research, self.football_data_fields)
        )
        counter["requests"] += 1
        field_counter.update(fields)

    def research_completed(self, result, *, latency_ms, category="external"):
        counter = self.research if category == "external" else self.football_data_research
        tokens = self.research_tokens if category == "external" else self.football_data_tokens
        latencies = self.research_latency_ms if category == "external" else self.football_data_latency_ms
        counter["web_search_calls"] += result.search_calls
        counter["tool_calls"] += result.tool_calls
        counter[f"transport:{result.transport}"] += 1
        resolved = {"FOUND", "CONFIRMED", "PROBABLE", "REPORTED"}
        counter["fields_found"] += sum(f.status.value in resolved for f in result.findings)
        counter["fields_not_found"] += sum(f.status.value not in resolved for f in result.findings)
        tokens["input"] += result.input_tokens
        tokens["output"] += result.output_tokens
        if result.estimated_cost is not None:
            if category == "external":
                self.research_cost += float(result.estimated_cost)
            else:
                self.football_data_cost += float(result.estimated_cost)
        latencies.append(latency_ms)
        if category != "external":
            return
        for finding in result.findings:
            self.research_source_tiers.update(source.reliability_tier.value for source in finding.sources)
        confidences = [finding.confidence for finding in result.findings]
        if confidences:
            self.research["confidence_sum_milli"] += round(sum(confidences) * 1000)
            self.research["confidence_count"] += len(confidences)

    def research_cache(self, hit, *, category="external"):
        counter = self.research if category == "external" else self.football_data_research
        counter["cache_hits" if hit else "cache_misses"] += 1

    def research_error(self, status):
        self.research[f"error:{status}"] += 1

    def snapshot(self):
        total = sum(self.decisions.values())
        cache_total = self.research["cache_hits"] + self.research["cache_misses"]
        confidence_count = self.research["confidence_count"]
        football_cache_total = (
            self.football_data_research["cache_hits"] + self.football_data_research["cache_misses"]
        )
        return {
            "decision_counts": dict(self.decisions),
            "no_bet_ratio": self.decisions["NO_BET"] / total if total else None,
            "agent_calls": dict(self.agent_calls),
            "agent_errors": dict(self.agent_errors),
            "agent_latency_ms": dict(self.agent_latency_ms),
            "external_research": {
                "requests": self.research["requests"],
                "web_search_calls": self.research["web_search_calls"],
                "tool_calls": self.research["tool_calls"],
                "transports": {
                    key.removeprefix("transport:"): value
                    for key, value in self.research.items()
                    if key.startswith("transport:")
                },
                "fields_requested": dict(self.research_fields),
                "fields_found": self.research["fields_found"],
                "fields_not_found": self.research["fields_not_found"],
                "source_tier_distribution": dict(self.research_source_tiers),
                "average_confidence": (
                    self.research["confidence_sum_milli"] / 1000 / confidence_count
                    if confidence_count
                    else None
                ),
                "cache_hit_rate": self.research["cache_hits"] / cache_total if cache_total else None,
                "tokens": dict(self.research_tokens),
                "estimated_cost": self.research_cost,
                "average_latency_ms": (
                    sum(self.research_latency_ms) / len(self.research_latency_ms)
                    if self.research_latency_ms
                    else None
                ),
                "errors": {
                    key.removeprefix("error:"): value
                    for key, value in self.research.items()
                    if key.startswith("error:")
                },
            },
            "football_data_research": {
                "requests": self.football_data_research["requests"],
                "tool_calls": self.football_data_research["tool_calls"],
                "web_search_calls": self.football_data_research["web_search_calls"],
                "fields_requested": dict(self.football_data_fields),
                "fields_found": self.football_data_research["fields_found"],
                "fields_not_found": self.football_data_research["fields_not_found"],
                "tokens": dict(self.football_data_tokens),
                "estimated_cost": self.football_data_cost,
                "average_latency_ms": (
                    sum(self.football_data_latency_ms) / len(self.football_data_latency_ms)
                    if self.football_data_latency_ms
                    else None
                ),
                "cache_hit_rate": self.football_data_research["cache_hits"] / football_cache_total
                if football_cache_total
                else None,
            },
        }
