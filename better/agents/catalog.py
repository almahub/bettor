"""User-facing metadata for the fourteen executable agent roles."""

AGENT_CATALOG = {
    "football_data_analyst": {
        "responsibility": "Verifica statistiche, identità, campione e copertura delle squadre.",
        "data_inputs": ("Statistiche normalizzate", "Storico recente", "Provenance statistica"),
        "output": "Profili casa/trasferta, confronti e avvisi sul campione",
    },
    "football_odds_analyst": {
        "responsibility": "Legge quote correnti e movimenti senza formulare giudizi di valore.",
        "data_inputs": ("Quote 888", "Mercati", "Timestamp e liquidità"),
        "output": "Quote correnti, movimenti e anomalie",
    },
    "news_intelligence": {
        "responsibility": "Deduplica e classifica notizie, affidabilità e conflitti.",
        "data_inputs": ("External Research", "Notizie pre-match", "Provenance delle fonti"),
        "output": "Eventi verificati, conflitti e notizie non confermate",
    },
    "lineup_player_impact": {
        "responsibility": "Distingue formazioni probabili e ufficiali e valuta la sensibilità disponibile.",
        "data_inputs": ("Formazioni", "Indisponibili", "Fonti ufficiali dei club"),
        "output": "Stato formazione e sensibilità all'incertezza",
    },
    "tactical_analyst": {
        "responsibility": "Confronta caratteristiche tattiche documentate delle due squadre.",
        "data_inputs": ("Profili statistici", "Formazioni", "Notizie"),
        "output": "Matchup tattici con limiti ed evidenze",
    },
    "predictive_model": {
        "responsibility": "Calcola distribuzioni probabilistiche deterministiche del risultato.",
        "data_inputs": ("Profili statistici", "Validazione indipendente"),
        "output": "1X2, Over/Under 2.5, BTTS e distribuzione punteggi",
    },
    "market_selection": {
        "responsibility": "Filtra i mercati in base a copertura, modello e validazione.",
        "data_inputs": ("Dati", "Quote", "Previsioni", "Validazione"),
        "output": "Mercati eleggibili e motivi di esclusione",
    },
    "value_bettor": {
        "responsibility": "Confronta probabilità ammesse e prezzi con matematica deterministica.",
        "data_inputs": ("Quote complete", "Ensemble validato", "Mercati eleggibili"),
        "output": "Fair odds, edge, EV e quota minima",
    },
    "model_validator": {
        "responsibility": "Valida i modelli su previsioni storiche concluse senza leakage.",
        "data_inputs": ("Storico out-of-sample", "Esiti", "Quote di chiusura"),
        "output": "Brier, log loss, calibrazione, CLV e stato del modello",
    },
    "risk_manager": {
        "responsibility": "Applica Kelly frazionario e limiti di esposizione del portafoglio.",
        "data_inputs": ("Candidate di valore", "Validazione", "Banca e posizioni"),
        "output": "Stake consentito, esposizioni e veto di rischio",
    },
    "devils_advocate": {
        "responsibility": "Cerca contraddizioni, dati scaduti e fragilità della proposta.",
        "data_inputs": ("Tutti i report specialistici", "Evidenze e degradazioni"),
        "output": "Criticità, severità e condizioni di risoluzione",
    },
    "professional_bettor": {
        "responsibility": "Esegue i gate deterministici e produce la decisione finale.",
        "data_inputs": ("Report specialistici", "Portafoglio", "Policy di decisione"),
        "output": "BET, NO_BET, WATCHLIST, attesa o dati insufficienti",
    },
    "betting_journal": {
        "responsibility": "Registra lo snapshot e la decisione in forma append-only.",
        "data_inputs": ("Decisione finale", "Identità analisi e partita"),
        "output": "Voce di journal immutabile e auditabile",
    },
    "matched_betting": {
        "responsibility": "Calcola coperture back/lay in un workflow separato.",
        "data_inputs": ("Quote back/lay", "Commissione", "Termini promozionali"),
        "output": "Stake di copertura e profitti nei due esiti",
    },
}
