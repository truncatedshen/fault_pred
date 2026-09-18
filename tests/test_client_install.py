"""多客户端安装：Codex（TOML）与 OpenCode（JSON）各自的配置格式、skill 位置与校验。

路径与格式都来自各自官方文档（OpenCode: `mcp.<name>` + `type: local` + `command` 数组；
skill 在 `~/.config/opencode/skills/<name>/` 或 `<项目>/.opencode/skills/<name>/`）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from install_mcp_config import (  # noqa: E402
    bridge_command,
    default_config_path,
    default_skill_dir,
    opencode_entry,
    read_entry,
    upsert_opencode,
)
from install_skill import parse_frontmatter, validate_skill  # noqa: E402
from verify_deploy import check_skill  # noqa: E402


def run_script(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    # 脚本会打印中文。中文 Windows 的子进程默认按 cp936 写 stdout，父进程按 utf-8 读会
    # UnicodeDecodeError（reader 线程炸掉后 stdout 变成 None）。约定子进程输出编码即可。
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )


def test_default_paths_match_the_documented_locations():
    assert default_config_path("opencode").as_posix().endswith(".config/opencode/opencode.json")
    assert default_config_path("codex").as_posix().endswith(".codex/config.toml")
    assert default_skill_dir("opencode").as_posix().endswith(".config/opencode/skills/fault-prediction")
    assert default_skill_dir("agents").as_posix().endswith(".agents/skills/fault-prediction")
    with pytest.raises(ValueError, match="Unknown client"):
        default_config_path("vscode")


def test_opencode_entry_uses_a_command_array():
    entry = opencode_entry("C:/py.exe", "http://127.0.0.1:8765")
    # OpenCode 的 command 是"命令 + 参数"的数组，type 必须是 local。
    assert entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][:3] == ["C:/py.exe", "-m", "fault_platform"]
    assert bridge_command(entry, "opencode") == (
        "C:/py.exe",
        ["-m", "fault_platform", "mcp", "--url", "http://127.0.0.1:8765"],
    )


def test_upsert_opencode_keeps_every_other_key():
    existing = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {"deepseek": {"options": {"apiKey": "secret-value"}}},
            "mcp": {"other": {"type": "remote", "url": "https://example.invalid/mcp"}},
        }
    )
    updated, action = upsert_opencode(existing, "fault-prediction", "C:/py.exe", "http://127.0.0.1:8765")
    assert action == "created"
    document = json.loads(updated)
    # 别人的 provider 段（含密钥）必须原样在，别人的 MCP 条目也要留着。
    assert document["provider"]["deepseek"]["options"]["apiKey"] == "secret-value"
    assert document["mcp"]["other"]["url"] == "https://example.invalid/mcp"
    assert document["mcp"]["fault-prediction"]["type"] == "local"

    again, action_again = upsert_opencode(updated, "fault-prediction", "C:/py.exe", "http://127.0.0.1:8765")
    assert action_again == "unchanged" and again == updated


def test_upsert_opencode_refuses_jsonc_and_broken_shapes():
    with pytest.raises(ValueError, match="not plain JSON"):
        upsert_opencode("// comment\n{}", "fault-prediction", "p", "u")
    with pytest.raises(ValueError, match="must be a JSON object"):
        upsert_opencode("[1, 2]", "fault-prediction", "p", "u")
    with pytest.raises(ValueError, match="'mcp' key"):
        upsert_opencode('{"mcp": []}', "fault-prediction", "p", "u")


def test_cli_writes_opencode_config_with_backup(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"$schema": "x", "model": "a/b"}), encoding="utf-8")
    completed = run_script(
        "install_mcp_config.py",
        "--client",
        "opencode",
        "--config",
        str(config),
        "--python",
        sys.executable,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "action: created" in completed.stdout
    assert "kept top-level keys" in completed.stdout
    document = json.loads(config.read_text(encoding="utf-8"))
    assert document["model"] == "a/b"
    assert document["mcp"]["fault-prediction"]["type"] == "local"
    backups = list(tmp_path.glob("opencode.json.bak-*"))
    assert len(backups) == 1
    # 备份必须是原文件的内容。
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"$schema": "x", "model": "a/b"}

    rerun = run_script(
        "install_mcp_config.py", "--client", "opencode", "--config", str(config), "--python", sys.executable
    )
    assert rerun.returncode == 0 and "unchanged" in rerun.stdout
    assert len(list(tmp_path.glob("opencode.json.bak-*"))) == 1  # 没有变化就不写第二次备份


def test_cli_refuses_to_edit_invalid_opencode_json(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text("{ this is not json", encoding="utf-8")
    completed = run_script(
        "install_mcp_config.py", "--client", "opencode", "--config", str(config), "--python", sys.executable
    )
    assert completed.returncode == 2
    assert "not valid JSON" in completed.stdout
    assert config.read_text(encoding="utf-8") == "{ this is not json"


def test_read_entry_normalises_both_clients(tmp_path):
    codex = tmp_path / "config.toml"
    codex.write_text(
        '[mcp_servers.fault-prediction]\ncommand = "C:/py.exe"\nargs = ["-m", "fault_platform", "mcp"]\n',
        encoding="utf-8",
    )
    assert bridge_command(read_entry(codex, "codex"), "codex") == (
        "C:/py.exe",
        ["-m", "fault_platform", "mcp"],
    )
    opencode = tmp_path / "opencode.json"
    opencode.write_text(
        json.dumps({"mcp": {"fault-prediction": opencode_entry("C:/py.exe", "http://x")}}), encoding="utf-8"
    )
    assert bridge_command(read_entry(opencode, "opencode"), "opencode")[0] == "C:/py.exe"

    with pytest.raises(ValueError, match="does not exist"):
        read_entry(tmp_path / "missing.json", "opencode")
    with pytest.raises(ValueError, match="no mcp.other entry"):
        read_entry(opencode, "opencode", "other")


def test_install_skill_copies_into_an_explicit_target(tmp_path):
    target = tmp_path / "skills" / "fault-prediction"
    completed = run_script(
        "install_skill.py",
        "--client",
        "opencode",
        "--target",
        str(target),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "action : created" in completed.stdout
    assert (target / "SKILL.md").exists() and (target / "references").is_dir()
    # 装完必须能通过部署验收里的那次 skill 检查（同一套判据）。
    assert check_skill(target)

    again = run_script("install_skill.py", "--client", "opencode", "--target", str(target))
    assert again.returncode == 0 and "action : replaced" in again.stdout
    assert check_skill(target)


def test_install_skill_rejects_bad_names_and_wrong_client_scope(tmp_path):
    # 名字与目录不一致（OpenCode 要求 name 必须等于目录名）。
    source = tmp_path / "wrong-name"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: something-else\ndescription: x\n---\n", encoding="utf-8")
    completed = run_script("install_skill.py", "--source", str(source), "--target", str(tmp_path / "out"))
    assert completed.returncode == 2
    assert "must match the directory name" in completed.stdout

    # Codex 没有项目级 skill 目录——不要编一个出来。
    refused = run_script("install_skill.py", "--client", "codex", "--scope", "project")
    assert refused.returncode == 2
    assert "global" in refused.stdout


def test_validate_skill_checks_frontmatter_rules():
    problems = validate_skill(ROOT / "skills" / "fault-prediction")
    assert problems == []
    fields = parse_frontmatter(
        (ROOT / "skills" / "fault-prediction" / "SKILL.md").read_text(encoding="utf-8")
    )
    assert fields["name"] == "fault-prediction"
    # OpenCode 的硬上限：description 1–1024 字符。
    assert 0 < len(fields["description"]) <= 1024


def test_codex_toml_entry_still_written_by_the_same_cli(tmp_path):
    """老客户端的路径不能被这次改动弄坏：TOML 仍是原样。"""
    config = tmp_path / "config.toml"
    completed = run_script(
        "install_mcp_config.py", "--client", "codex", "--config", str(config), "--python", sys.executable
    )
    assert completed.returncode == 0
    entry = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["fault-prediction"]
    assert entry["args"][:2] == ["-m", "fault_platform"] and entry["startup_timeout_sec"] == 60
