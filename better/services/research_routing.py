"""Disjoint routing between the two saved research agents."""

from better.services.football_data_research import FOOTBALL_DATA_FIELDS

EXTERNAL_RESEARCH_FIELDS = frozenset(
    {
        "injuries",
        "suspensions",
        "unavailable_players",
        "doubtful_players",
        "returned_players",
        "probable_lineups",
        "confirmed_lineups",
        "squad_callups",
        "manager_press_conferences",
        "manager_changes",
        "turnover",
        "fixture_congestion",
        "relevant_match_news",
        "travel_context",
        "weather",
        "pitch_playing_conditions",
        "contextual_information",
        "news",
    }
)
FOOTBALL_DATA_RESEARCH_FIELDS = frozenset(FOOTBALL_DATA_FIELDS)

if EXTERNAL_RESEARCH_FIELDS & FOOTBALL_DATA_RESEARCH_FIELDS:
    raise RuntimeError("research agent scopes must remain disjoint")


def route_research_field(field):
    if field in EXTERNAL_RESEARCH_FIELDS:
        return "external_research"
    if field in FOOTBALL_DATA_RESEARCH_FIELDS:
        return "football_data_research"
    return None
