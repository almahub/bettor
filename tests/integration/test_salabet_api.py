from fastapi.testclient import TestClient

from better.api.app import create_app
from better.services.application import make_application
from better.storage.repository import Repository


def test_bet_creates_one_authoritative_candidate_and_ui_exposes_manual_states():
    runtime = make_application(demo="bet", repository=Repository("sqlite:///:memory:"))
    app = create_app(runtime)
    salabet_routes = [
        (route.path, tuple(sorted(route.methods or ())))
        for route in app.routes
        if getattr(route, "path", "").startswith("/salabet")
    ]
    assert len(salabet_routes) == len(set(salabet_routes))
    with TestClient(app) as client:
        run = client.post("/demo/analysis", json={"scenario": "bet"}).json()
        forecast = run["forecast"]["markets"][0]
        selection = next(iter(forecast["probabilities"]))
        prediction = next(
            item
            for item in run["results"]["predictive_model"]["payload"]["model_predictions"]
            if selection in item["probabilities"].get(forecast["market"], {})
        )
        payload = {
            "analysis_id": run["analysis_id"],
            "prediction_id": prediction["prediction_id"],
            "market": forecast["market"],
            "selection": selection,
        }
        first = client.post("/salabet/candidates", json=payload)
        second = client.post("/salabet/candidates", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert len(client.get("/salabet/candidates").json()) == 1

        tampered = client.post(
            "/salabet/candidates", json={**payload, "probability": 0.999, "fair_odds": 1.001}
        )
        assert tampered.status_code == 422

        page = client.get("/salabet")
        script = client.get("/assets/salabet.mjs")
        assert page.status_code == script.status_code == 200
        assert "SalaBet · Bettor" in page.text
        assert "CONFERMA PUNTATA" in script.text
        assert "Schedina creata e spostata in Scommesse aperte" in script.text
        assert "La partita è già iniziata. Il live betting non è supportato." in script.text
        assert "Mostra rimossi" in script.text
        assert "data-hide-slip" in script.text and "data-restore-slip" in script.text
        assert "${response.status}:" not in script.text
        dashboard_script = client.get("/assets/dashboard.mjs").text
        assert ">ADD BET</button>" in dashboard_script
        assert 'data-action="bet-candidate"' not in dashboard_script
        assert "BET INDICATIVO" not in dashboard_script
        assert "bookmaker API" not in script.text

        blocked = client.post("/demo/analysis", json={"scenario": "no_bet"}).json()
        blocked_forecast = blocked["forecast"]["markets"][0]
        blocked_selection = next(iter(blocked_forecast["probabilities"]))
        blocked_prediction = next(
            item
            for item in blocked["results"]["predictive_model"]["payload"]["model_predictions"]
            if blocked_selection in item["probabilities"].get(blocked_forecast["market"], {})
        )
        rejected = client.post(
            "/salabet/candidates",
            json={
                "analysis_id": blocked["analysis_id"],
                "prediction_id": blocked_prediction["prediction_id"],
                "market": blocked_forecast["market"],
                "selection": blocked_selection,
            },
        )
        assert rejected.status_code == 409
        assert "ADD_BET_NOT_ELIGIBLE" in rejected.json()["detail"]

        raw = next(row for row in blocked["forecast"]["markets"] if row["method"] == "RAW_POISSON")
        leading = max(raw["probabilities"], key=raw["probabilities"].get)
        indicative_prediction = next(
            item for item in blocked["results"]["predictive_model"]["payload"]["model_predictions"]
            if leading in item["probabilities"].get(raw["market"], {})
        )
        indicative_payload = {
            "analysis_id": blocked["analysis_id"],
            "prediction_id": indicative_prediction["prediction_id"],
            "market": raw["market"],
            "selection": leading,
        }
        bank_before = client.get("/bank").json()["event_count"]
        indicative = client.post("/salabet/candidates/indicative", json=indicative_payload)
        duplicate = client.post("/salabet/candidates/indicative", json=indicative_payload)
        assert indicative.status_code == duplicate.status_code == 200
        assert indicative.json()["id"] == duplicate.json()["id"]
        assert indicative.json()["operationally_eligible"] is False
        assert len(client.get("/salabet/slips").json()) == 0
        assert client.get("/bank").json()["event_count"] == bank_before
        other = next(name for name in raw["probabilities"] if name != leading)
        invalid = client.post(
            "/salabet/candidates/indicative", json={**indicative_payload, "selection": other}
        )
        assert invalid.status_code == 409
        assert "INDICATIVE_SELECTION_MISMATCH" in invalid.json()["detail"]

        candidate_id = indicative.json()["id"]
        removed = client.post(
            f"/salabet/candidates/{candidate_id}/remove",
            json={"request_id": "f4a5a604-694b-453b-bc89-edc26dac5551"},
        )
        assert removed.status_code == 200
        assert all(row["id"] != candidate_id for row in client.get("/salabet/candidates").json())
        restored = client.post(
            f"/salabet/candidates/{candidate_id}/restore",
            json={"request_id": "9beea317-58d2-4c21-9b35-672a296cedf0"},
        )
        assert restored.status_code == 200
        assert restored.json()["id"] == candidate_id
        assert any(row["id"] == candidate_id for row in client.get("/salabet/candidates").json())
