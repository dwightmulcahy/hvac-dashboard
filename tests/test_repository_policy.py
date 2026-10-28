"""Repository/CI policy checks (ported from repo-template)."""

import pathlib
import re

ROOT = pathlib.Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_external_actions_are_pinned_to_commit_shas():
    for path in WORKFLOWS.glob("*.yml"):
        for ref in re.findall(r"uses:\s+([^\s#]+)", path.read_text(encoding="utf-8")):
            if ref.startswith("./"):
                continue
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), f"{path.name}: mutable action reference {ref}"


def test_workflows_default_to_read_only_token():
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE), path.name


def test_ci_gates_on_critical_and_high():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    gate = ci[ci.index("Fail build on") :]
    assert "severity: CRITICAL,HIGH" in gate
    assert 'exit-code: "1"' in gate


def test_release_moves_floating_tags_only_for_latest_stable():
    wf = (WORKFLOWS / "docker-release.yml").read_text(encoding="utf-8")
    assert "latest=false" in wf
    assert "type=semver,pattern={{version}}" in wf
    for alias in ("publish_major", "publish_minor", "is_latest"):
        assert wf.count(f"enable=${{{{ needs.verify.outputs.{alias} == 'true' }}}}") == 1
    assert "merge-base --is-ancestor" in wf
    assert "uses: ./.github/workflows/tests.yml" in wf


def test_dockerignore_admits_every_copy_source():
    """.dockerignore is an allowlist; a new COPY without an entry breaks the build."""
    allowed = [
        line[1:].rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.startswith("!")
    ]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for src in re.findall(r"^COPY\s+(?!--)(\S+)\s+\S+$", dockerfile, re.MULTILINE):
        src = src.rstrip("/")
        assert src in allowed, f"COPY {src} not admitted by .dockerignore"
