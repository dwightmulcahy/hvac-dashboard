"""Tests for scripts/build_changelog.py against a real throwaway git repository."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_changelog.py"
spec = importlib.util.spec_from_file_location("build_changelog", SCRIPT)
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def run(*a):
        subprocess.run(["git", *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")

    def commit(msg, tag=None):
        run("commit", "-q", "--allow-empty", "-m", msg)
        if tag:
            run("tag", tag)
    commit("feat: first feature", "v0.1.0")
    commit("feat(ui): add tabs (#12)")
    commit("fix: crash on start")
    commit("ci: faster builds")
    commit("feat!: new config format")
    commit("fix: old flag\n\nBREAKING CHANGE: removed --x")
    commit("just a message", "v0.2.0")
    commit("docs: readme", "v0.3.0-rc1")
    return tmp_path


def test_build(repo):
    data = bc.build("v0.2.0", "me/app")
    assert data["current"] == "0.2.0" and data["repository"] == "me/app"
    new, old = data["releases"]
    assert (new["version"], new["previous"], old["previous"]) == ("0.2.0", "v0.1.0", None)
    sections = {s["type"]: [i["text"] for i in s["items"]] for s in new["sections"]}
    assert sections == {"breaking": ["old flag", "new config format"],  # newest first
                        "feat": ["ui: add tabs (#12)"], "fix": ["crash on start"]}
    assert new["internal"] == 2 and len(new["date"]) == 10
    assert old["sections"][0]["items"][0]["text"] == "first feature"


def test_prerelease_lists_earlier_stable_releases(repo):
    assert [r["version"] for r in bc.build("v0.3.0-rc1")["releases"]] == ["0.2.0", "0.1.0"]
    with pytest.raises(ValueError):
        bc.build("latest")


def test_main_writes_json(repo):
    bc.main(["--upto", "v0.1.0", "--output", "out/changelog.json"])
    data = json.loads(Path("out/changelog.json").read_text())
    assert [r["tag"] for r in data["releases"]] == ["v0.1.0"] and data["repository"] == ""
