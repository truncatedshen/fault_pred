"""把 Agent skill 装到客户端能发现的位置，并在拷贝前先校验它本身是合法的。

    python scripts/install_skill.py --client opencode                 # ~/.config/opencode/skills
    python scripts/install_skill.py --client opencode --scope project  # ./.opencode/skills
    python scripts/install_skill.py --client codex                    # ~/.codex/skills
    python scripts/install_skill.py --client agents                   # ~/.agents/skills（两个客户端都读）

三个客户端的位置来自各自官方文档：

| 客户端 | 位置 |
| --- | --- |
| Codex | `$CODEX_HOME/skills/<name>/SKILL.md`（默认 `~/.codex/skills`） |
| OpenCode | `~/.config/opencode/skills/<name>/`（全局）或 `<项目>/.opencode/skills/<name>/` |
| agents | `~/.agents/skills/<name>/`——Codex 与 OpenCode **都**会读，装一份两边都能用 |

校验规则按最严的那个客户端（OpenCode）来：`name` 必须与目录同名、只含小写字母数字与单个连字符，
`description` 1–1024 字符。装之前先验，装不进去就明确报错，而不是留一个"看起来存在但加载不了"的目录。
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DESCRIPTION_LIMIT = 1024
REPO_SKILL = Path(__file__).resolve().parents[1] / "skills" / "fault-prediction"
CLIENTS = ("codex", "opencode", "agents")


def skill_root(client: str, scope: str, home: Path, cwd: Path) -> Path:
    """客户端 + 作用域 → skill 根目录。写死的路径都来自官方文档，不猜。"""
    if client == "codex":
        if scope != "global":
            raise ValueError(
                "Codex reads skills from $CODEX_HOME/skills (global) only; "
                "use --scope global, or --client agents / opencode for a project-local copy"
            )
        return home / ".codex" / "skills"
    if client == "agents":
        if scope != "global":
            raise ValueError("--client agents only has a global location (~/.agents/skills)")
        return home / ".agents" / "skills"
    if client == "opencode":
        return (
            (cwd / ".opencode" / "skills")
            if scope == "project"
            else (home / ".config" / "opencode" / "skills")
        )
    raise ValueError(f"Unknown client: {client}")


def parse_frontmatter(text: str) -> dict[str, str]:
    """只认最朴素的 `key: value` 形式——够校验 name/description，不引入 YAML 依赖。"""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("'\"")
    return fields


def validate_skill(source: Path) -> list[str]:
    """返回问题列表（空列表 = 可以安装）。规则按最严的客户端来。"""
    problems: list[str] = []
    document = source / "SKILL.md"
    if not document.exists():
        return [f"{document} not found (the file name must be SKILL.md, all caps)"]
    fields = parse_frontmatter(document.read_text(encoding="utf-8"))
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not name:
        problems.append("frontmatter is missing 'name'")
    else:
        if not NAME_PATTERN.match(name):
            problems.append(f"name {name!r} must be lowercase alphanumeric with single hyphens")
        if name != source.name:
            problems.append(f"name {name!r} must match the directory name {source.name!r}")
    if not description:
        problems.append("frontmatter is missing 'description'")
    elif len(description) > DESCRIPTION_LIMIT:
        problems.append(f"description is {len(description)} characters (limit {DESCRIPTION_LIMIT})")
    return problems


def install(source: Path, target: Path, root: Path) -> str:
    """把 skill 目录整份替换到 ``target``；返回一句话描述做了什么。"""
    resolved_source = source.resolve()
    resolved_target = target.resolve()
    resolved_root = root.resolve()
    if resolved_target == resolved_source:
        raise ValueError("source and target are the same directory; nothing to install")
    # 只在"目标确实是 <root>/<name>"时才删旧目录，避免把别处的东西删掉。
    if resolved_target.parent != resolved_root or resolved_target.name != source.name:
        raise ValueError(f"refusing to replace {resolved_target}: it is not {resolved_root}/{source.name}")
    action = "replaced" if resolved_target.exists() else "created"
    if resolved_target.exists():
        shutil.rmtree(resolved_target)
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(resolved_source, resolved_target)
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the fault-prediction Agent skill")
    parser.add_argument("--client", choices=CLIENTS, default="codex")
    parser.add_argument("--scope", choices=("global", "project"), default="global")
    parser.add_argument("--source", default="", help=f"Skill directory; default {REPO_SKILL}")
    parser.add_argument("--target", default="", help="Explicit target directory (overrides client/scope)")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    source = Path(arguments.source).expanduser() if arguments.source else REPO_SKILL
    if not source.exists():
        print(f"FAIL: skill directory not found: {source}")
        return 2
    problems = validate_skill(source)
    if problems:
        print("FAIL: the skill would not load:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    if arguments.target:
        # 显式给了目录就以它的父目录为根：安全校验仍然要求"目标就是 <父目录>/<skill 名>"。
        target = Path(arguments.target).expanduser()
        root = target.parent
    else:
        try:
            root = skill_root(arguments.client, arguments.scope, Path.home(), Path.cwd())
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 2
        target = root / source.name

    print(f"client : {arguments.client} ({arguments.scope})")
    print(f"source : {source.resolve()}")
    print(f"target : {target.resolve()}")
    if arguments.dry_run:
        print("wrote  : nothing (--dry-run)")
        return 0
    try:
        action = install(source, target, root)
    except (ValueError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 2
    files = sum(1 for path in target.rglob("*") if path.is_file())
    print(f"action : {action}")
    print(f"wrote  : {target} ({files} files)")
    print("note   : restart the client session — skills are discovered at startup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
