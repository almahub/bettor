"""Explicit league profile migration; never changes budgets or automation switches."""

from copy import deepcopy

SERIE_A = "888it:competition:1000095001"
SERIE_B = "888it:competition:1000095002"
PREMIER = "888it:competition:1000094985"


def serie_a_premier(app, sources):
    app, sources = deepcopy(app), deepcopy(sources)
    app["active_competitions"] = [SERIE_A, PREMIER]
    app["timing_comparison_competitions"] = [SERIE_A, PREMIER]
    if "validation_verified_competitions" in app:
        app["validation_verified_competitions"] = [SERIE_A, PREMIER]
    catalog = sources["sport_888"]
    catalog["catalog_hours"] = 336
    catalog["allowed_competition_patterns"] = ["^Serie A$", "^Premier League$"]
    catalog["allowed_competitions"] = {1000095001: "^Serie A$", 1000094985: "^Premier League$"}
    api = sources["api_football"]
    api["competitions"] = {SERIE_A: {"league": 135, "season": 2026}, PREMIER: {"league": 39, "season": 2026}}
    api["archive_leagues"] = [135, 39]
    # Existing native evidence remains readable; this migration adds no paid
    # provider collection. New observations use the configured free sources.
    api["network_enabled"] = False
    api["results_enabled"] = False
    sources["football_data_research"]["supported_competitions"] = ["Serie A", "Premier League"]
    free = sources["free_football"]
    free["competitions"] = {SERIE_A: "I1", PREMIER: "E0"}
    free["openfootball_divisions"] = ["I1", "E0"]
    free["csv_divisions"] = ["I1", "E0"]
    # Reviewed name variants from the actual CSV, OpenFootball, 888 and
    # football-data.org catalogs. Identity still requires competition, oriented
    # teams and kickoff; these are not guessed fixture mappings.
    free.setdefault("team_aliases", {}).update({
        "afc bournemouth": "bournemouth", "bournemouth afc": "bournemouth",
        "brighton hove albion": "brighton", "tottenham hotspur": "tottenham",
        "brighton hove": "brighton", "nottingham": "nottingham forest", "leicester": "leicester city",
        "hull city afc": "hull city", "sunderland afc": "sunderland",
        "man city": "manchester city", "man united": "manchester united",
        "nott m forest": "nottingham forest", "nott forest": "nottingham forest",
        "newcastle": "newcastle united", "leeds": "leeds united", "ipswich": "ipswich town",
        "coventry": "coventry city", "hull": "hull city", "tottenham": "tottenham",
        "west ham": "west ham united", "wolves": "wolverhampton wanderers",
    })
    sources["football_data_org_competitions"] = {
        SERIE_A: "football-data:competition:2019", PREMIER: "football-data:competition:2021"}
    return app, sources
