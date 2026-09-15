"""Deployment helpers: idempotent MCP registration and bundle inputs."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from install_mcp_config import SERVER, upsert  # noqa: E402

EXISTING = """model = "deepseek"

[mcp_servers]

[mcp_servers.node_repl]
command = 'C:\\node.exe'
args = []

[projects.'d:\\x']
trust_level = "trusted"
"""


def test_upsert_creates_entry_and_keeps_everything_else():
    updated, action = upsert(EXISTING, SERVER, "C:\\venv\\python.exe", "http://127.0.0.1:8765", 60)
    assert action == "created"
    parsed = tomllib.loads(updated)
    assert parsed["model"] == "deepseek"
    assert list(parsed["mcp_servers"]) == ["node_repl", SERVER]
    assert parsed["projects"]["d:\\x"]["trust_level"] == "trusted"
    assert parsed["mcp_servers"][SERVER]["command"] == "C:\\venv\\python.exe"
    assert parsed["mcp_servers"][SERVER]["args"][:3] == ["-m", "fault_platform", "mcp"]


def test_upsert_is_idempotent_and_updates_in_place():
    first, _ = upsert("", SERVER, "C:\\a\\python.exe", "http://127.0.0.1:8765", 60)
    again, action = upsert(first, SERVER, "C:\\a\\python.exe", "http://127.0.0.1:8765", 60)
    assert action == "unchanged" and again == first

    moved, action = upsert(first, SERVER, "D:\\b\\python.exe", "http://127.0.0.1:8766", 90)
    assert action == "updated"
    entry = tomllib.loads(moved)["mcp_servers"][SERVER]
    assert entry["command"] == "D:\\b\\python.exe"
    assert entry["startup_timeout_sec"] == 90
    assert "http://127.0.0.1:8766" in entry["args"]
    assert moved.count(f"[mcp_servers.{SERVER}]") == 1  # never duplicates the table


def test_upsert_into_file_without_mcp_section():
    updated, action = upsert('model = "x"\n', SERVER, "C:\\a\\python.exe", "http://127.0.0.1:8765", 60)
    assert action == "created"
    parsed = tomllib.loads(updated)
    assert parsed["model"] == "x" and SERVER in parsed["mcp_servers"]


def test_cli_refuses_to_edit_invalid_toml(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[broken\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "install_mcp_config.py"),
            "--config",
            str(config),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 2
    assert "not valid TOML" in completed.stdout
    assert config.read_text(encoding="utf-8") == "[broken\n"  # never damaged


def test_cli_writes_backup_and_entry(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(EXISTING, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "install_mcp_config.py"),
            "--config",
            str(config),
            "--python",
            sys.executable,
            "--url",
            "http://127.0.0.1:9999",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "backup" in completed.stdout
    backups = list(tmp_path.glob("config.toml.bak-*"))
    assert len(backups) == 1
    assert tomllib.loads(backups[0].read_text(encoding="utf-8")) == tomllib.loads(EXISTING)
    entry = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"][SERVER]
    assert entry["command"] == sys.executable
    assert "9999" in " ".join(entry["args"])


def test_cli_accepts_a_utf8_bom_and_normalises_it(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(b"\xef\xbb\xbf" + ('model = "x"\n').encode("utf-8"))
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "install_mcp_config.py"),
            "--config",
            str(config),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "removed the UTF-8 BOM" in completed.stdout
    raw = config.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert SERVER in tomllib.loads(raw.decode("utf-8"))["mcp_servers"]


@pytest.mark.parametrize("name", ["deploy.ps1", "verify_deploy.py", "mcp_smoke.py", "install_mcp_config.py"])
def test_release_inputs_exist(name):
    assert (Path(__file__).resolve().parents[1] / "scripts" / name).exists()


def test_cli_stores_an_absolute_interpreter_path(tmp_path):
    """A relative --python would break when the client starts the bridge elsewhere."""
    config = tmp_path / "config.toml"
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    completed = subprocess.run(
        [
            sys.executable,
            str(scripts / "install_mcp_config.py"),
            "--config",
            str(config),
            "--python",
            "scripts/../scripts/../.venv/Scripts/python.exe",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    entry = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"][SERVER]
    assert Path(entry["command"]).is_absolute()
    assert ".." not in entry["command"]
