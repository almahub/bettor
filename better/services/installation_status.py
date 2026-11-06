"""Read-only setup diagnostics: missing configuration is never called a provider outage."""

from decimal import Decimal

from better.services.credential_store import CredentialStore


def installation_status(runtime, values=None):
    values = CredentialStore().values() if values is None else values
    issues = []

    def add(code, title, message, href="#system/settings"):
        issues.append({"code": code, "title": title, "message": message, "href": href})

    if not values.get("OPENAI_API_KEY"):
        add(
            "OPENAI_KEY_MISSING",
            "Chiave OpenAI mancante",
            "Le ricerche AI non sono disponibili. Archivio e calcoli locali restano utilizzabili se hanno dati sufficienti.",
        )
    for name, label in [
        ("OPENAI_RESEARCH_AGENT_ID", "External Research"),
        ("OPENAI_FOOTBALL_DATA_AGENT_ID", "Football Data Research"),
    ]:
        if not values.get(name):
            add(
                "RESEARCH_AGENT_NOT_CONFIGURED",
                label + " non configurato",
                "Inserisci l’ID del saved agent per abilitare questa ricerca.",
            )
    if not values.get("FOOTBALL_DATA_ORG_API_KEY"):
        add(
            "RESULT_PROVIDER_KEY_MISSING",
            "football-data.org non configurato",
            "Aggiornamenti di risultati e classifiche da questo provider non disponibili. Il settlement richiede comunque un risultato verificato.",
        )
    repository = getattr(runtime, "repository", None)
    if repository is not None:
        from sqlalchemy import select

        from better.services.bank_service import BankService
        from better.storage.db import TABLES

        bank = BankService(repository).status()
        if not bank["initialized"] or Decimal(bank["available"]) <= 0:
            add(
                "BANK_FUNDS_MISSING",
                "Banca senza fondi disponibili",
                "Nessun fondo viene caricato automaticamente. Configura la Banca ed effettua un versamento prima di registrare giocate.",
                "/salabet#bank",
            )
        with repository.engine.connect() as connection:
            has_history = any(
                connection.execute(select(TABLES[name].c.id).limit(1)).first() is not None
                for name in ("api_football_archive", "historical_observations", "team_match_stats")
            )
        if not has_history:
            add(
                "LOCAL_HISTORY_EMPTY",
                "Archivio storico vuoto",
                "Importa un backup o acquisisci i dati necessari. Le analisi senza un campione sufficiente resteranno limitate.",
                "#system/backup",
            )
    return {
        "configured": not issues,
        "issues": issues,
        "note": "Configurazione locale: non verifica autenticazione, credito o copertura dei provider. Consulta API Control per la verifica effettiva.",
    }
