"""Register the fault-prediction MCP server in a client's config file, idempotently.

    python scripts/install_mcp_config.py --client codex
    python scripts/install_mcp_config.py --client opencode

两个客户端各有自己的配置格式；本脚本只碰自己那一段，其余内容保留，写入前先备份：

| `--client` | 文件（默认） | 格式 |
| --- | --- | --- |
| `codex` | `~/.codex/config.toml` | TOML 表 `[mcp_servers.<name>]` |
| `opencode` | `~/.config/opencode/opencode.json` | JSON 的 `mcp.<name>`（`type: local`） |

写入前都会重新解析一遍结果，证明文件仍然合法；解析不了就**拒绝写**，而不是留一个坏文件。
OpenCode 也接受 JSONC（带注释），但本脚本只处理纯 JSON——遇到注释会明确报错并让你手工加，
因为"猜着去改带注释的文件"很容易毁掉用户自己的注释。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

SERVER = "fault-prediction"
CLIENTS = ("codex", "opencode")
#: OpenCode 的配置 schema（写新文件时带上，编辑器据此补全）。
OPENCODE_SCHEMA = "https://opencode.ai/config.json"


def default_config_path(client: str) -> Path:
    """各客户端的默认配置文件位置（全局作用域）。"""
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "config.toml"
    if client == "opencode":
        return home / ".config" / "opencode" / "opencode.json"
    raise ValueError(f"Unknown client: {client}")


# --------------------------------------------------------------------------- Codex (TOML)


def _block(name: str, python: str, url: str, startup_timeout: int) -> str:
    return (
        f"[mcp_servers.{name}]\n"
        f'args = ["-m", "fault_platform", "mcp", "--url", "{url}"]\n'
        f"command = '{python}'\n"
        f"startup_timeout_sec = {startup_timeout}\n"
    )


def _section_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"^\[mcp_servers\.{re.escape(name)}\]\s*$(?:\n(?!\[)[^\n]*)*", re.MULTILINE)


def upsert(text: str, name: str, python: str, url: str, startup_timeout: int) -> tuple[str, str]:
    """Return (new_text, action) where action is 'created', 'updated' or 'unchanged'."""
    block = _block(name, python, url, startup_timeout)
    pattern = _section_pattern(name)
    match = pattern.search(text)
    if match:
        if match.group(0).strip() == block.strip():
            return text, "unchanged"
        # A callable replacement avoids re.sub interpreting backslashes in Windows paths.
        updated = pattern.sub(lambda _: block.rstrip("\n") + "\n", text, count=1)
        return updated, "updated"
    separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return f"{text}{separator}{block}", "created"


# --------------------------------------------------------------------------- OpenCode (JSON)


def opencode_entry(python: str, url: str) -> dict[str, Any]:
    """OpenCode 的本地 MCP 条目：``command`` 是**数组**（命令 + 参数），``type`` 必须是 ``local``。"""
    return {
        "type": "local",
        "command": [python, "-m", "fault_platform", "mcp", "--url", url],
        "enabled": True,
    }


def upsert_opencode(text: str, name: str, python: str, url: str) -> tuple[str, str]:
    """把条目写进 OpenCode 的 JSON 配置，返回 ``(新文本, action)``。

    只支持纯 JSON：OpenCode 也接受 JSONC，但带注释时无法在不破坏注释的前提下安全改写，
    所以这里直接拒绝并让人手工加，而不是猜。
    """
    entry = opencode_entry(python, url)
    if not text.strip():
        document = {"$schema": OPENCODE_SCHEMA, "mcp": {name: entry}}
        return json.dumps(document, indent=2, ensure_ascii=False) + "\n", "created"
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"the config is not plain JSON ({exc.msg} at line {exc.lineno}); OpenCode also accepts "
            "JSONC, but this installer will not rewrite a file with comments — add the block by hand, "
            "or point --config at a plain .json file"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError("the OpenCode config must be a JSON object")
    servers = document.get("mcp")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise ValueError("the 'mcp' key in the OpenCode config must be an object")
    if servers.get(name) == entry:
        return text, "unchanged"
    action = "updated" if name in servers else "created"
    servers[name] = entry
    document["mcp"] = servers
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n", action


# --------------------------------------------------------------------------- CLI


def _entry_of(client: str, text: str, name: str) -> dict[str, Any] | None:
    if client == "codex":
        return tomllib.loads(text).get("mcp_servers", {}).get(name)
    return json.loads(text).get("mcp", {}).get(name)


def default_skill_dir(client: str, server: str = SERVER) -> Path:
    """该客户端默认从哪里加载 skill（装/验两边必须一致）。"""
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "skills" / server
    if client == "opencode":
        return home / ".config" / "opencode" / "skills" / server
    if client == "agents":
        return home / ".agents" / "skills" / server
    raise ValueError(f"Unknown client: {client}")


def read_entry(config: Path, client: str, name: str = SERVER) -> dict[str, Any]:
    """读回某个客户端的 MCP 条目；读不到就抛 ``ValueError`` 并说清是哪个文件。"""
    if not config.exists():
        raise ValueError(f"{config} does not exist")
    entry = _entry_of(client, config.read_text(encoding="utf-8-sig"), name)
    if not entry:
        where = "mcp_servers" if client == "codex" else "mcp"
        raise ValueError(f"{config} has no {where}.{name} entry")
    return entry


def bridge_command(entry: dict[str, Any], client: str) -> tuple[str, list[str]]:
    """把两种格式的条目归一成 ``(解释器, 参数列表)``——Codex 是 command+args，OpenCode 是一个数组。"""
    if client == "codex":
        return str(entry["command"]), [str(item) for item in entry.get("args", [])]
    command = entry.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("the opencode 'command' must be a non-empty array (command + args)")
    return str(command[0]), [str(item) for item in command[1:]]


def _valid(text: str, client: str) -> str:
    """返回空串表示合法，否则返回错误描述（用于"先校验再写"）。"""
    try:
        tomllib.loads(text) if client == "codex" else json.loads(text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        return str(exc)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the fault-prediction MCP server")
    parser.add_argument("--client", choices=CLIENTS, default="codex")
    parser.add_argument("--config", default="", help="Config file; empty uses the client default")
    parser.add_argument("--python", default=sys.executable, help="Interpreter the MCP bridge should run")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Fault platform service URL")
    parser.add_argument("--name", default=SERVER)
    parser.add_argument("--startup-timeout", type=int, default=60, help="codex only")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    config = (
        Path(arguments.config).expanduser() if arguments.config else default_config_path(arguments.client)
    )
    # Resolve once: a relative interpreter would break when the client launches the
    # bridge from another working directory.
    python = Path(arguments.python).expanduser()
    if not python.is_absolute():
        python = (Path.cwd() / python).resolve()
    if not python.exists():
        print(f"FAIL: interpreter not found: {python}")
        return 2
    # utf-8-sig tolerates a BOM, which Windows editors and PowerShell's Set-Content add.
    text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    had_bom = config.exists() and config.read_bytes().startswith(b"\xef\xbb\xbf")
    if text:
        problem = _valid(text, arguments.client)
        if problem:
            # 措辞按客户端说清楚：老版本对 codex 用的是 "not valid TOML"，保持这句不变。
            label = "TOML" if arguments.client == "codex" else "JSON"
            print(f"FAIL: existing config is not valid {label}, refusing to edit: {problem}")
            return 2
    try:
        if arguments.client == "codex":
            updated, action = upsert(
                text, arguments.name, str(python), arguments.url, arguments.startup_timeout
            )
        else:
            updated, action = upsert_opencode(text, arguments.name, str(python), arguments.url)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    problem = _valid(updated, arguments.client)
    if problem:  # pragma: no cover - guarded by construction
        print(f"FAIL: produced invalid config: {problem}")
        return 2
    entry = _entry_of(arguments.client, updated, arguments.name)
    if not entry:
        print("FAIL: entry missing after edit")
        return 2

    print(f"client: {arguments.client}")
    print(f"config: {config}")
    print(f"action: {action}")
    print(f"entry : {json.dumps(entry, ensure_ascii=False)}")
    if arguments.dry_run:
        print("---- would write ----")
        print(updated, end="")
        print("wrote : nothing (--dry-run)")
        return 0
    if arguments.client == "opencode" and action != "unchanged":
        # 说清我们对文件做了什么：JSON 会被重新格式化，其余顶层键一个不动。
        others = sorted(set(json.loads(updated)) - {"mcp"})
        print(f"note  : JSON rewritten pretty-printed (2-space indent); kept top-level keys: {others}")
    if action == "unchanged":
        print("wrote : nothing (already current)")
        return 0
    if config.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config.with_name(f"{config.name}.bak-{stamp}")
        shutil.copy2(config, backup)
        print(f"backup: {backup}")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(updated, encoding="utf-8")
    print(f"wrote : {config}")
    if had_bom:
        print("note  : removed the UTF-8 BOM while writing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
