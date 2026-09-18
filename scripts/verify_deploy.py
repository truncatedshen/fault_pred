"""Acceptance test for a deployed Fault Prediction Platform.

Run it on the target machine after installing:

    python scripts/verify_deploy.py --from-config                    # Codex (~/.codex/config.toml)
    python scripts/verify_deploy.py --from-config --client opencode  # OpenCode (~/.config/opencode)

It checks the environment, the installed skill, the HTTP service (UI + SSE), and drives
a complete pipeline through MCP using the *configured* bridge command. Exit code 0 means
the deployment can be handed to an Agent or a user.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from install_mcp_config import (  # noqa: E402
    bridge_command,
    default_config_path,
    default_skill_dir,
    read_entry,
)

CHECKS: list[dict[str, Any]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, attempts: int = 120) -> bool:
    for _ in range(attempts):
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.25)
    return False


def check_environment() -> bool:
    ok = record("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        from fault_platform.registry import default_registry

        components = len(default_registry().list(limit=500))
        ok &= record("fault_platform import + registry", components >= 29, f"{components} components")
    except Exception as exc:  # pragma: no cover - reported instead of raised
        ok &= record("fault_platform import + registry", False, str(exc))
    for module, hint in (("mcp", "Agent bridge"), ("pyarrow", "Parquet input"), ("xgboost", "XGBoost")):
        try:
            __import__(module)
            record(f"optional dependency: {module}", True, hint)
        except ImportError:
            record(f"optional dependency: {module}", True, f"absent ({hint} unavailable)")
    return ok


def check_skill(skill_dir: Path) -> bool:
    skill = skill_dir / "SKILL.md"
    if not skill.exists():
        return record("skill installed", False, str(skill))
    text = skill.read_text(encoding="utf-8")
    has_front_matter = (
        text.startswith("---") and "name:" in text.split("---")[1] and "description:" in text.split("---")[1]
    )
    name = ""
    for line in text.split("---")[1].splitlines():
        if line.strip().startswith("name:"):
            name = line.split(":", 1)[1].strip()
    matches = name == skill_dir.name
    body = "MCP" in text and "execute_pipeline" in text
    return record(
        "skill installed and well formed",
        has_front_matter and matches and body,
        f"{skill} name={name} dir={skill_dir.name} body_mentions_mcp={body}",
    )


def check_config(config_path: Path, server: str, client: str) -> tuple[bool, dict[str, Any] | None]:
    """读配置里的 MCP 条目，并把解释器与参数归一化出来（两种客户端格式都支持）。"""
    if not config_path.exists():
        return record("mcp config present", False, str(config_path)), None
    try:
        entry = read_entry(config_path, client, server)
        python, args = bridge_command(entry, client)
    except (ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        return record("mcp config entry readable", False, str(exc)), None
    command = Path(python)
    ok = record("mcp config entry readable", True, json.dumps(entry, ensure_ascii=False))
    ok &= record("configured interpreter exists", command.exists(), str(command))
    return ok, {"command": python, "args": args, "client": client}


def start_service(python: Path, data_root: Path, storage_root: Path):
    """Launch the service; the caller keeps it alive for the HTTP *and* MCP checks."""
    port = free_port()
    log = (storage_root.parent / "verify-service.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "fault_platform",
            "serve",
            "--port",
            str(port),
            "--data-root",
            str(data_root),
            "--storage-root",
            str(storage_root),
            "--artifact-cache-mb",
            "256",
        ],
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    base = f"http://127.0.0.1:{port}"
    if not wait_for(base + "/api/health"):
        process.terminate()
        log.close()
        return None, port, log.name
    return process, port, log.name


def stop_service(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


def check_service(base: str) -> bool:
    ok = record("service starts and answers /api/health", True, base)
    with httpx.Client(trust_env=False, timeout=10) as client:
        health = client.get(base + "/api/health").json()
        ok &= record("registry exposed over HTTP", health["components"] >= 29, str(health["components"]))
        ok &= record("designer page served", client.get(base + "/").status_code == 200)
        ok &= record("static assets served", client.get(base + "/static/app.js").status_code == 200)
        with client.stream("GET", base + "/api/events") as response:
            first = next(response.iter_lines())
            ok &= record(
                "live event stream (SSE) available",
                response.status_code == 200 and first.startswith(": connected"),
                response.headers.get("content-type", ""),
            )
    return ok


def check_mcp(
    scripts: Path, python: Path, url: str, from_config: bool, client: str, config_path: Path
) -> bool:
    smoke = scripts / "mcp_smoke.py"
    if not smoke.exists():
        return record("MCP end-to-end smoke available", False, str(smoke))
    command = [str(python), str(smoke), "--url", url, "--client", client]
    if from_config:
        command += ["--from-config", "--config", str(config_path)]
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    output = completed.stdout.strip()
    try:
        report = json.loads(output[output.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return record("MCP end-to-end pipeline", False, (completed.stderr or output)[-300:])
    return record(
        "MCP end-to-end pipeline",
        completed.returncode == 0 and report.get("success"),
        f"tools={report.get('tools')} status={report.get('status')} "
        f"accuracy={report.get('metrics', {}).get('accuracy')} {time.perf_counter() - started:.0f}s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deployed Fault Prediction Platform")
    parser.add_argument(
        "--from-config", action="store_true", help="Use the interpreter and URL from config.toml"
    )
    parser.add_argument("--client", choices=("codex", "opencode", "agents"), default="codex")
    parser.add_argument("--config", default="", help="Config file; empty uses the client default")
    parser.add_argument("--server", default="fault-prediction")
    parser.add_argument("--skill-dir", default="")
    parser.add_argument("--work-dir", default="", help="Scratch directory for the verification run")
    parser.add_argument("--json-report", default="")
    arguments = parser.parse_args()

    config_path = (
        Path(arguments.config).expanduser() if arguments.config else default_config_path(arguments.client)
    )
    skill_dir = (
        Path(arguments.skill_dir).expanduser()
        if arguments.skill_dir
        else default_skill_dir(arguments.client, arguments.server)
    )
    work = (
        Path(arguments.work_dir).expanduser()
        if arguments.work_dir
        else Path.cwd() / ".fault-platform" / f"verify-{int(time.time())}"
    )
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "storage").mkdir(parents=True, exist_ok=True)
    print(f"work dir: {work}\n")

    entry: dict[str, Any] | None = None
    python = Path(sys.executable)
    ok = check_environment()
    ok &= check_skill(skill_dir)
    if arguments.from_config:
        config_ok, entry = check_config(config_path, arguments.server, arguments.client)
        ok &= config_ok
        if entry:
            python = Path(entry["command"])
    process, port, log_path = start_service(python, work / "data", work / "storage")
    if process is None:
        ok &= record("service starts and answers /api/health", False, f"see {log_path}")
    else:
        try:
            ok &= check_service(f"http://127.0.0.1:{port}")
            ok &= check_mcp(
                Path(__file__).resolve().parent,
                python,
                f"http://127.0.0.1:{port}",
                arguments.from_config,
                arguments.client,
                config_path,
            )
        finally:
            stop_service(process)

    failed = [check for check in CHECKS if not check["ok"]]
    report = {
        "success": not failed,
        "python": sys.executable,
        "skill_dir": str(skill_dir),
        "config": str(config_path) if arguments.from_config else None,
        "bridge_command": entry["command"] if entry else None,
        "checks": CHECKS,
    }
    if arguments.json_report:
        Path(arguments.json_report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("failed:", ", ".join(check["check"] for check in failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
