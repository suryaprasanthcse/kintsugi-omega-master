from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi import PINNED_TOOLCHAIN, __version__
from kintsugi.cli import EXIT_FAIL, EXIT_OK, EXIT_SKIPPED, main
from kintsugi.proof.attest import load_public_key

REPO = Path(__file__).resolve().parents[1]


def test_version_prints_pinned_toolchain(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    out = capsys.readouterr().out
    assert __version__ in out
    for tool, version in PINNED_TOOLCHAIN.items():
        assert tool in out
        assert version in out


def test_the_demo_replays_a_sealed_sabotage_and_its_audit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """End to end, offline: seal → T0 → deliver → anchor → reveal → audit C1 and C6 PASS."""
    from kintsugi.sandbox import proc

    if proc.which("git") is None:
        pytest.skip("git is not installed")
    assert main(["demo", "--root", str(tmp_path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "C1  PASS" in out
    assert "C6  PASS" in out
    assert "audit exit code: 0" in out
    assert "not exercised offline" in out  # what it cannot check is said, never passed
    assert not any(tmp_path.iterdir())  # the scratch practice repo is removed


def test_the_demo_fails_when_its_audit_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kintsugi import demo

    failed = {"run": "demo-x", "duration_s": 0.1, "ledger": [], "anchor": "anchor-x",
              "audit": {"exit_code": 1, "results": {"C1": {"status": "FAIL", "summary": "x"}}},
              "not_exercised": {}, "disclosed": []}  # fmt: skip
    monkeypatch.setattr(demo, "run", lambda root, keep, log: failed)
    assert main(["demo"]) == EXIT_FAIL
    assert "C1  FAIL" in capsys.readouterr().out


def test_audit_before_t0_skips_what_needs_the_window(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exit code 2 is the spec's "checks skipped" (§7.5): never report a pass that did not run.
    monkeypatch.chdir(REPO)
    if (REPO / ".kintsugi" / "window.json").exists():
        pytest.skip("T0 has happened: the window record exists")
    assert main(["audit", "--all", "--offline", "./published-bundles", "--json"]) == EXIT_SKIPPED
    import json

    checks = {c["check"]: c["status"] for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["C1"] == checks["C2"] == checks["C7"] == "SKIP"
    assert checks["C11"] == "PASS"


def test_keys_generate_prints_only_the_public_half(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private, public = tmp_path / "custody" / "attest_ed25519", tmp_path / "attest.pub"
    argv = ["keys", "generate", "--private-key", str(private), "--public-key", str(public)]
    assert main(argv) == EXIT_OK
    out = capsys.readouterr().out
    assert "PRIVATE KEY" not in out
    assert private.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public.read_text().startswith("ssh-ed25519 ")
    load_public_key(public)
    assert "fingerprint SHA256:" in out


def test_keys_generate_refuses_a_private_key_inside_a_repo(tmp_path: Path) -> None:
    inside = REPO / "attest_ed25519.must-not-exist"
    argv = ["keys", "generate", "--private-key", str(inside), "--public-key", str(tmp_path / "p")]
    assert main(argv) == EXIT_FAIL
    assert not inside.exists()


def test_keys_generate_never_overwrites(tmp_path: Path) -> None:
    private = tmp_path / "attest_ed25519"
    argv = ["keys", "generate", "--private-key", str(private), "--public-key", str(tmp_path / "p")]
    assert main(argv) == EXIT_OK
    before = private.read_bytes()
    assert main(argv) == EXIT_FAIL
    assert private.read_bytes() == before


def test_fleet_down_asks_a_running_fleet_to_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["fleet", "down"]) == EXIT_OK
    assert (tmp_path / ".state" / "runtime" / "STOP").is_file()


def test_the_committed_holdout_manifest_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    assert main(["holdout", "verify"]) == EXIT_OK


def test_senses_provision_dry_run_renders_every_receptor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO)
    assert main(["senses", "provision", "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.count("neatlogs  R-") == 12
    assert out.count("omega     omega:R-") == 9
    assert "R-007 ledger v1" in out


def test_senses_ingress_needs_provisioned_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kintsugi.senses import service
    from kintsugi.senses.store import SenseStore

    with pytest.raises(FileNotFoundError, match="senses provision"):
        service.ingress_app(tmp_path, SenseStore(tmp_path / "s.sqlite"))
    made = service.ingress_secrets(tmp_path, create=True)
    assert len(made.path_secret) >= 43
    assert made.path_secret not in repr(made)
    assert service.ingress_secrets(tmp_path) == made
    assert len({made.path_secret, made.signing_secret, made.admin_token}) == 3


def test_pre_stage5_ingress_secrets_gain_an_admin_token(tmp_path: Path) -> None:
    import json

    from kintsugi.senses import service

    path = tmp_path / service.SECRET_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"path_secret": "p" * 43, "signing_secret": "s" * 43}), "utf-8")
    first = service.ingress_secrets(tmp_path)
    assert len(first.admin_token) >= 43
    assert service.ingress_secrets(tmp_path).admin_token == first.admin_token  # kept


def test_control_deadman_pages_without_a_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["control", "deadman"]) == EXIT_FAIL
    assert "DEAD" in capsys.readouterr().out
    assert (tmp_path / ".state" / "watchdog" / "pages.jsonl").is_file()


def test_holdout_keygen_refuses_an_identity_inside_a_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO)
    inside = REPO / "holdout.agekey.must-not-exist"
    assert main(["holdout", "keygen", "--identity", str(inside)]) == EXIT_FAIL
    assert not inside.exists()
    assert "refusing" in capsys.readouterr().out
