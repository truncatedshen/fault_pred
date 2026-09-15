"""Register the MCP server in a Codex config.toml, idempotently.

    python scripts/install_mcp_config.py --python C:\\path\\to\\.venv\\Scripts\\python.exe

Only the ``[mcp_servers.fault-prediction]`` table is touched: every other key in the
file is preserved byte for byte, a timestamped backup is written before the first
change, and the result is re-parsed with tomllib to prove it is still valid TOML.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

SERVER = "fault-prediction"


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the fault-prediction MCP server")
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--python", default=sys.executable, help="Interpreter the MCP bridge should run")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Fault platform service URL")
    parser.add_argument("--name", default=SERVER)
    parser.add_argument("--startup-timeout", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    config = Path(arguments.config).expanduser()
    python = Path(arguments.python).expanduser()
    if not python.exists():
        print(f"FAIL: interpreter not found: {python}")
        return 2
    # utf-8-sig tolerates a BOM, which Windows editors and PowerShell's Set-Content add.
    text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    had_bom = config.exists() and config.read_bytes().startswith(b"\xef\xbb\xbf")
    if text:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            print(f"FAIL: existing config is not valid TOML, refusing to edit: {exc}")
            return 2
    updated, action = upsert(text, arguments.name, str(python), arguments.url, arguments.startup_timeout)
    try:
        parsed = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - guarded by construction
        print(f"FAIL: produced invalid TOML: {exc}")
        return 2
    entry = parsed.get("mcp_servers", {}).get(arguments.name)
    if not entry:
        print("FAIL: entry missing after edit")
        return 2
    others = sorted(set(parsed.get("mcp_servers", {})) - {arguments.name})
    print(f"config: {config}")
    print(f"action: {action}")
    print(f"entry : command={entry['command']} args={entry['args']}")
    print(f"others: {others}")
    if arguments.dry_run or action == "unchanged":
        print("wrote : nothing" if arguments.dry_run else "wrote : nothing (already current)")
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
