"""Read-only T30 notices against each selection's actual saved analysis."""

from datetime import timedelta

from better.domain.enums import DecisionStatus
from better.services.timing_comparison import ENTITY, GRACE, moment


class SlipCheckpointReview:
    def __init__(self, repository, settings, clock):
        self.repository, self.settings, self.clock = repository, settings, clock
        self.rows = repository.all_records("annotations", entity_id=ENTITY)

    def selection(self, selection, bookmaker):
        due = selection.kickoff - timedelta(minutes=30)
        result = {"match_id": selection.match_id, "home_team": selection.home_team,
                  "away_team": selection.away_team, "scheduled_at": due.isoformat(),
                  "initial_analysis_id": str(selection.analysis_id), "analysis_id": None,
                  "status": "PENDING", "notices": [], "initial_odds":
                  float(selection.odds_at_analysis) if selection.odds_at_analysis else None,
                  "updated_odds": None, "checked_at": None}
        rows = [r for r in self.rows if r["match_id"] == selection.match_id
                and r["checkpoint"] == "T30" and moment(r["kickoff"]) == selection.kickoff]
        capture = next((r for r in rows if r["kind"] == "CAPTURE"), None)
        if capture is None:
            result["status"] = ("FAILED" if any(r["kind"] == "FAILED" for r in rows)
                                else "MISSING" if self.clock() >= due + GRACE
                                else "IN_PROGRESS" if any(r["kind"] == "CLAIM" for r in rows)
                                else "PENDING")
            return result
        initial = self.repository.get_run(selection.analysis_id)
        current = self.repository.get_run(capture["analysis_id"])
        if (initial is None or current is None or initial.as_of > current.as_of
                or current.match_id != selection.match_id
                or current.snapshot.match.kickoff != selection.kickoff
                or current.snapshot.match.competition != selection.competition_id
                or (current.snapshot.match.home_team_id, current.snapshot.match.away_team_id) != selection.team_ids
                or not due <= current.as_of <= current.created_at <= moment(capture["at"]) < due + GRACE
                or moment(capture["at"]) > self.clock()):
            result["status"] = "INVALID_CHECKPOINT"
            return result
        snapshots = [current.snapshot.stats, current.snapshot.news, current.snapshot.lineups]
        if any(s.captured_at > current.as_of or any(not p.available_at(current.as_of) for p in s.provenance)
               for s in snapshots if s is not None) or any(
                   q.captured_at > current.as_of or not q.provenance.available_at(current.as_of)
                   for q in (current.snapshot.odds.quotes if current.snapshot.odds else ())):
            result["status"] = "INVALID_CHECKPOINT"
            return result
        result.update(analysis_id=str(current.analysis_id), checked_at=current.as_of.isoformat())
        def notice(code, message):
            result["notices"].append({"code": code, "message": message})
        original_report = initial.results.get("predictive_model")
        original_model = next((p for p in getattr(getattr(original_report, "payload", None), "model_predictions", ())
                               if p.prediction_id == selection.prediction_id), None)
        report = current.results.get("predictive_model")
        models = getattr(getattr(report, "payload", None), "model_predictions", ())
        model = next((p for p in models if original_model and p.model == original_model.model
                      and p.model_version == original_model.model_version), None)
        probabilities = model.probabilities.get(selection.market, {}) if model and model.analysis_available else {}
        if model and model.data_cutoff > current.as_of:
            result["status"] = "INVALID_CHECKPOINT"
            return result
        exact_bet = bool(model and model.operationally_eligible and current.decision.status == DecisionStatus.BET
                         and current.decision.market is not None and current.decision.market.type == selection.market
                         and current.decision.market.selection == selection.selection)
        initial_probabilities = original_model.probabilities.get(selection.market, {}) if original_model else {}
        was_leading = bool(initial_probabilities and max(initial_probabilities, key=initial_probabilities.get) == selection.selection)
        if not probabilities or selection.selection not in probabilities:
            result["status"] = "NOT_USABLE"
            notice("MODEL_INPUT_UNAVAILABLE", "Proposta non più verificabile con lo stesso modello e versione.")
        elif exact_bet:
            result["status"] = "CONFIRMED"
        elif was_leading and max(probabilities, key=probabilities.get) != selection.selection:
            result["status"] = "CHANGED"
            notice("SELECTION_CHANGED", "Il modello ora indica un altro esito: riesaminare la selezione.")
        else:
            result["status"] = "NOT_ADMITTED" if model.operationally_eligible else "INDICATIVE"
            if not model.operationally_eligible:
                notice("NOT_ADMITTED", "Pronostico confermato solo come indicativo; BET operativo non ammesso.")
            else:
                notice("NOT_ADMITTED", "Proposta non ammessa dal Decision Engine al controllo T−30: " + current.decision.status.value)
        quotes = [q for q in current.snapshot.odds.quotes if q.market.type == selection.market
                  and q.market.selection == selection.selection and q.bookmaker == bookmaker and not q.is_opening
                  and 0 <= (current.as_of - q.captured_at).total_seconds() <= self.settings.odds_ttl_seconds] if current.snapshot.odds else []
        quote = max(quotes, key=lambda q: q.captured_at, default=None)
        if quote:
            result["updated_odds"] = float(quote.odds)
            result["quote_captured_at"] = quote.captured_at.isoformat()
            if selection.odds_at_analysis is not None and quote.odds != selection.odds_at_analysis:
                notice("QUOTE_CHANGED", "Quota cambiata rispetto all'analisi iniziale, sullo stesso bookmaker.")
        else:
            notice("QUOTE_UNAVAILABLE", "Quota aggiornata non verificabile per questa selezione e bookmaker.")
        a, b = initial.snapshot.lineups, current.snapshot.lineups
        impact = current.results.get("lineup_player_impact")
        official = bool(b and getattr(getattr(impact, "payload", None), "official", False))
        if not official:
            notice("LINEUP_PENDING", "Formazione ufficiale non verificata al controllo T−30.")
        elif a is None or not a.home_players or not a.away_players:
            notice("LINEUP_UPDATED", "Formazione ufficiale ora disponibile; confronto iniziale non completo.")
        elif (set(a.home_players), set(a.away_players), set(a.unavailable)) != (set(b.home_players), set(b.away_players), set(b.unavailable)):
            relevant = b.sensitivity >= self.settings.lineup_sensitivity_threshold
            notice("LINEUP_MATERIAL_CHANGE" if relevant else "LINEUP_CHANGED",
                   "Formazione cambiata con sensibilità rilevante: riesaminare l'analisi." if relevant else "Formazione o disponibilità cambiate rispetto all'analisi iniziale.")
        if capture.get("missing"):
            notice("DATA_PARTIAL", "Controllo T−30 con dati mancanti: consultare l'analisi.")
        if result["status"] == "CONFIRMED" and result["notices"]:
            result["status"] = "REVIEW_REQUIRED"
        return result

    def review(self, slip):
        if slip.status not in {"DRAFT", "READY", "PLACED", "OPEN"}:
            return None
        rows = [self.selection(s, slip.bookmaker) for s in slip.selections
                if s.competition_id in self.settings.timing_comparison_competitions]
        if not rows:
            return None
        statuses = {r["status"] for r in rows}
        status = "REVIEW_REQUIRED" if statuses - {"PENDING", "IN_PROGRESS", "CONFIRMED"} else (
            "PENDING" if statuses & {"PENDING", "IN_PROGRESS"} else "CONFIRMED")
        return {"status": status, "selections": rows, "note":
                "Avviso sullo snapshot T−30, non verifica live. Selezioni, quote accettate, importi e Banca invariati. Aggiorna per leggere gli ultimi checkpoint."}
