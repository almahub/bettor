import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from better import __version__


@pytest.mark.parametrize("corrupt", [False, True])
def test_remote_assets_verified_before_publication_and_retention(tmp_path, monkeypatch, corrupt):
    spec = importlib.util.spec_from_file_location(
        "publish_release_checked", Path(__file__).parents[2] / "scripts/publish-release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["publish-release", "--tag", "v" + __version__])
    folder = tmp_path / "dist/installer"
    folder.mkdir(parents=True)
    files = {}
    for prefix in ("Better", "Bettor"):
        name = f"{prefix}-Setup-{__version__}-x64.exe"
        content = b"test installer fixture"
        files[name] = content
        files[name + ".sha256"] = (hashlib.sha256(content).hexdigest() + "  " + name + "\n").encode()
    for name, content in files.items():
        (folder / name).write_bytes(content)
    notes = tmp_path / f"docs/releases/{__version__}.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Release notes")
    calls = []

    def gh(*args):
        calls.append(args[:2])
        if args[:2] == ("release", "download"):
            destination = Path(args[args.index("--dir") + 1])
            for name, content in files.items():
                (destination / name).write_bytes(b"corrupted" if corrupt else content)
        if args[:2] == ("release", "view"):
            if args[-1] == "assets":
                return json.dumps({"assets": [{"name": name} for name in files]})
            return json.dumps({"isDraft": False, "isPrerelease": False, "tagName": "v" + __version__})
        return ""

    monkeypatch.setattr(module, "gh", gh)

    def api(*args, **kwargs):
        calls.append(("api", "list"))
        releases = [{"tag_name": "v" + __version__, "published_at": "2026-11-06", "created_at": "2026-11-06"}]
        releases += [
            {"tag_name": f"v0.0.{i}", "published_at": None, "created_at": f"2026-10-0{i}"}
            for i in range(1, 4)
        ]
        return SimpleNamespace(stdout=json.dumps([releases]))

    monkeypatch.setattr(module.subprocess, "run", api)
    if corrupt:
        with pytest.raises(RuntimeError, match="non verificato"):
            module.main()
        assert ("release", "edit") not in calls
        assert ("release", "delete") not in calls
    else:
        module.main()
        assert (
            calls.index(("release", "download"))
            < calls.index(("release", "edit"))
            < calls.index(("api", "list"))
            < calls.index(("release", "delete"))
        )
