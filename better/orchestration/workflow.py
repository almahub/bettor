from better.orchestration.dependency_graph import topological_layers

# Validation is intentionally performed BEFORE the ensemble, so suspended models never
# acquire influence. It reads only independent historical outcomes, never current predictions.
SPORTS_DEPENDENCIES = {
    "football_data_analyst": (),
    "football_odds_analyst": (),
    "news_intelligence": (),
    "lineup_player_impact": (),
    "model_validator": (),
    "tactical_analyst": ("football_data_analyst", "lineup_player_impact", "news_intelligence"),
    "predictive_model": ("football_data_analyst", "model_validator"),
    "market_selection": (
        "football_data_analyst",
        "football_odds_analyst",
        "predictive_model",
        "model_validator",
    ),
    "value_bettor": ("football_odds_analyst", "predictive_model", "market_selection", "model_validator"),
    "risk_manager": ("value_bettor", "model_validator"),
    "devils_advocate": (
        "value_bettor",
        "football_data_analyst",
        "football_odds_analyst",
        "news_intelligence",
        "lineup_player_impact",
        "predictive_model",
        "model_validator",
        "tactical_analyst",
    ),
    "professional_bettor": (
        "football_data_analyst",
        "football_odds_analyst",
        "news_intelligence",
        "lineup_player_impact",
        "tactical_analyst",
        "predictive_model",
        "market_selection",
        "value_bettor",
        "model_validator",
        "risk_manager",
        "devils_advocate",
    ),
    "betting_journal": ("professional_bettor",),
}
SPORTS_LAYERS = topological_layers(SPORTS_DEPENDENCIES)
MATCHED_BETTING_DEPENDENCIES = {"matched_betting": ()}
