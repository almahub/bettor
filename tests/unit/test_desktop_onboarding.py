from pathlib import Path
from types import SimpleNamespace

import pytest

from better.desktop import launcher, onboarding
from better.desktop.window import backend_command
from better.services.bank_service import BankService
from better.services.credential_store import CredentialStore
from better.services.data_backup import create_backup
from better.services.installation_status import installation_status
from better.storage.repository import Repository
from tests.unit.test_data_backup import amount, home_at


def test_new_installation_has_no_money_slips_or_credentials(tmp_path, monkeypatch):
    home = tmp_path / "new"
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-should-not-be-used")
    assert not onboarding.ready(home)
    onboarding.prepare(home, "new", launcher.initialize)
    assert onboarding.ready(home)
    assert not CredentialStore(home).values().get("OPENAI_API_KEY")
    assert launcher.os.environ["OPENAI_API_KEY"] == ""
    assert CredentialStore(home).values()["BETTER_PIPELINE_AUTO_COLLECT"] == "false"
    repo = Repository("sqlite:///" + (home / "data/888-real.db").as_posix())
    assert BankService(repo).status()["available"] == "0"
    assert not BankService(repo).status()["initialized"]
    for table in ("bet_slips", "bet_candidates", "analysis_runs", "validation_observations"):
        assert repo.list_records(table) == []
    codes = {item["code"] for item in installation_status(SimpleNamespace(repository=repo), {})["issues"]}
    assert {
        "OPENAI_KEY_MISSING",
        "RESEARCH_AGENT_NOT_CONFIGURED",
        "RESULT_PROVIDER_KEY_MISSING",
        "BANK_FUNDS_MISSING",
        "LOCAL_HISTORY_EMPTY",
    } <= codes
    repo.engine.dispose()


def test_existing_choice_keeps_database_and_new_choice_protects_it(tmp_path):
    home = home_at(tmp_path / "existing")
    onboarding.prepare(home, "existing", lambda _: None)
    assert amount(home) == 42
    result = onboarding.prepare(home, "new", launcher.initialize)
    assert result["safety_backup"]
    assert (home / "backups" / result["safety_backup"]).exists()
    assert not (home / "data/888-real.db").exists()
    previous = next((home / "backups").glob("first-run-previous-*"))
    assert amount(previous) == 42


def test_restore_in_empty_home_keeps_real_data_and_disables_collector(tmp_path):
    source = home_at(tmp_path / "source", value=73)
    archive = create_backup(source)
    target = tmp_path / "empty"
    onboarding.prepare(target, "restore", launcher.initialize, archive)
    assert onboarding.ready(target)
    assert amount(target) == 73
    assert CredentialStore(target).values()["BETTER_PIPELINE_AUTO_COLLECT"] == "false"


def test_failed_reset_rolls_back_without_marking_ready(tmp_path):
    home = home_at(tmp_path / "home")

    def broken(path):
        (path / "data").mkdir()
        raise RuntimeError("initialization failure")

    with pytest.raises(RuntimeError):
        onboarding.prepare(home, "new", broken)
    assert amount(home) == 42
    assert not onboarding.ready(home)


def test_cancel_and_background_do_not_initialize_empty_home(tmp_path, monkeypatch):
    home = tmp_path / "empty"
    monkeypatch.setattr("sys.argv", ["Better", "--data-dir", str(home), "--no-browser"])
    launcher.main()
    assert not (home / "data").exists()
    assert not (home / "config").exists()
    monkeypatch.setattr("sys.argv", ["Better", "--data-dir", str(home)])
    monkeypatch.setattr(onboarding, "choose", lambda _: None)
    launcher.main()
    assert not onboarding.ready(home)


def test_distribution_is_windowed_and_uses_hidden_service():
    spec = Path("packaging/Better.spec").read_text()
    assert "console=False" in spec
    assert "--no-browser" in backend_command(Path("."), 8019)
    source = Path("better/desktop/launcher.py").read_text(encoding="utf-8")
    assert "webbrowser.open" not in source
    assert "Settings.load(home /" in source


def test_desktop_shutdown_requires_local_token_and_calls_stop_only_when_idle(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from better.api.settings import mount_settings

    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    engine = SimpleNamespace(settings=SimpleNamespace(database_url="sqlite:///data/test.db"), _locks={})
    mount_settings(app, lambda: engine)
    stopped = []
    app.state.desktop_stop = lambda: stopped.append(True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1)) as client:
        path = "/system/settings/backups/desktop-stop"
        assert client.post(path).status_code == 403
        headers = {"x-better-settings-token": client.get("/system/settings").json()["token"]}
        app.state.data_pipeline_collector = SimpleNamespace(running=True)
        assert client.post(path, headers=headers).status_code == 409
        assert not stopped
        app.state.data_pipeline_collector.running = False
        assert client.post(path, headers=headers).status_code == 200
        assert stopped == [True]


@pytest.mark.asyncio
async def test_runtime_closes_after_collector_on_the_same_event_loop():
    import asyncio
    from contextlib import asynccontextmanager

    events = []
    loop = asyncio.get_running_loop()

    @asynccontextmanager
    async def original(app):
        events.append("start")
        yield
        events.append("collector-stopped")

    async def close():
        assert asyncio.get_running_loop() is loop
        events.append("providers-closed")

    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=original))
    launcher.own_runtime(app, SimpleNamespace(close=close))
    async with app.router.lifespan_context(app):
        events.append("running")
    assert events == ["start", "running", "collector-stopped", "providers-closed"]
