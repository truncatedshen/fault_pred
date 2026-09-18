"""Build a self-contained deployment bundle for another machine.

    python scripts/export_release.py            # reuse an existing wheel
    python scripts/export_release.py --build    # build the wheel first

Produces ``dist/fault-prediction-platform-<version>-deploy.zip`` containing the wheel,
the Agent skill, the deployment/verification scripts and the documentation. The target
machine only needs Python 3.11+.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "deploy.ps1",
    "verify_deploy.py",
    "mcp_smoke.py",
    "install_mcp_config.py",
    "install_skill.py",
    "browser_check.cjs",
)
DOCS = ("deploy.md", "mcp.md", "components.md", "design.md", "validation.md", "architecture.md")
EXTRAS = ("README.md", "pyproject.toml", "requirements-win-py311.lock")


def build_wheel() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "-w", "dist", "--no-deps", "-q"],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the deployment bundle")
    parser.add_argument("--build", action="store_true", help="Build the wheel before packaging")
    parser.add_argument("--output", default=str(ROOT / "dist"))
    arguments = parser.parse_args()

    if arguments.build:
        build_wheel()
    wheels = sorted((ROOT / "dist").glob("*.whl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not wheels:
        raise SystemExit("no wheel found in dist/; run with --build")
    wheel = wheels[0]
    version = wheel.stem.split("-")[1] if "-" in wheel.stem else "0.0.0"

    output = Path(arguments.output)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"fault-prediction-platform-{version}-deploy.zip"
    manifest: list[str] = []

    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging) / f"fault-prediction-platform-{version}-deploy"
        (root / "dist").mkdir(parents=True)
        shutil.copy2(wheel, root / "dist" / wheel.name)
        manifest.append(f"dist/{wheel.name}")

        skill_source = ROOT / "skills" / "fault-prediction"
        if not (skill_source / "SKILL.md").exists():
            raise SystemExit(f"skill missing: {skill_source}")
        shutil.copytree(skill_source, root / "skills" / "fault-prediction")
        manifest.extend(
            path.relative_to(root).as_posix()
            for path in sorted((root / "skills").rglob("*"))
            if path.is_file()
        )

        (root / "scripts").mkdir(exist_ok=True)
        (root / "docs").mkdir(exist_ok=True)
        for name in SCRIPTS:
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, root / "scripts" / name)
                manifest.append(f"scripts/{name}")
        for name in DOCS:
            source = ROOT / "docs" / name
            if source.exists():
                shutil.copy2(source, root / "docs" / name)
                manifest.append(f"docs/{name}")
        for name in EXTRAS:
            source = ROOT / name
            if source.exists():
                shutil.copy2(source, root / name)
                manifest.append(name)

        quickstart = f"""# Fault Prediction Platform {version} - deployment bundle

Everything needed for a machine that has only Python 3.11+.

## 1. Install (Windows)

```powershell
Expand-Archive fault-prediction-platform-{version}-deploy.zip -DestinationPath .
cd fault-prediction-platform-{version}-deploy
powershell -ExecutionPolicy Bypass -File scripts\\deploy.ps1
```

The script creates a virtual environment under `%LOCALAPPDATA%\\fault-prediction-platform`,
installs the wheel with the MCP and Parquet extras, installs the Agent skill into
`%USERPROFILE%\\.codex\\skills\\fault-prediction`, registers the MCP server in
`%USERPROFILE%\\.codex\\config.toml`, and finally runs the acceptance test.

## 2. Run

```powershell
%LOCALAPPDATA%\\fault-prediction-platform\\start.ps1
```

Open http://127.0.0.1:8765, click "加载示例" then "运行方案".

## 3. Verify (any time)

```powershell
%LOCALAPPDATA%\\fault-prediction-platform\\.venv\\Scripts\\python.exe `
  %LOCALAPPDATA%\\fault-prediction-platform\\scripts\\verify_deploy.py --from-config
```

## 4. Use it from an Agent

Restart the Codex session so the MCP server and the skill are picked up, then ask for a
pipeline ("用统计特征和随机森林搭一个故障预测方案"). Full guide: `docs/deploy.md`.

## Contents

{chr(10).join(f"- {entry}" for entry in manifest)}
"""
        (root / "DEPLOY.md").write_text(quickstart, encoding="utf-8")
        manifest.append("DEPLOY.md")

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    # One top-level folder, so expanding the archive yields a single directory.
                    bundle.write(path, f"{root.name}/{path.relative_to(root).as_posix()}")

    print(f"bundle: {archive} ({archive.stat().st_size / 1024:.0f} KB)")
    print("contents:")
    for entry in manifest:
        print(f"  - {entry}")


if __name__ == "__main__":
    main()
