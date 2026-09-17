"""The Agent skill must stay true to the live tool surface and the component registry.

The skill is what an agent reads before touching the platform, so a renamed tool, a dropped
component or a truncated guide is a real defect: the agent would hallucinate around the gap.
These checks are cheap and fail loudly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fault_platform.registry import default_registry
from fault_platform.service import CONTROL_OPERATIONS

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "fault-prediction"
CATEGORIES = ("data", "feature", "explore", "visual", "validation")
#: A tool call in prose looks like ``name(`` with no dotted prefix (that would be a component
#: method such as ``pandas.read_csv(``).
CALL = re.compile(r"(?<![\w.])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(")
#: Component references are ``category.name``; wildcards such as ``visual.*`` carry no name,
#: and a file name such as ``data.csv`` is not a component.
COMPONENT = re.compile(r"(?<![\w.])(?:" + "|".join(CATEGORIES) + r")\.[a-z_]+")
FILE_SUFFIXES = {"csv", "parquet", "pq", "py", "md", "json", "xml", "txt", "xlsx", "mat", "log"}


def _markdown_files() -> list[Path]:
    return sorted(SKILL_DIR.rglob("*.md"))


def test_skill_files_exist_and_are_substantial() -> None:
    skill = SKILL_DIR / "SKILL.md"
    assert skill.exists(), f"missing {skill}"
    lines = skill.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 200, "SKILL.md regressed to a thin summary"
    for name, minimum in (
        ("stages.md", 200),
        ("recipes.md", 80),
        ("troubleshooting.md", 80),
        ("components.md", 120),
    ):
        reference = SKILL_DIR / "references" / name
        assert reference.exists(), f"missing reference {reference}"
        assert len(reference.read_text(encoding="utf-8").splitlines()) >= minimum


def test_front_matter_declares_the_directory_name() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front_matter = text.split("---", 2)[1]
    assert f"name: {SKILL_DIR.name}" in front_matter
    assert "description:" in front_matter and "MCP" in front_matter


def test_every_control_operation_is_documented() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    missing = sorted(name for name in CONTROL_OPERATIONS if name not in text)
    assert not missing, f"tools missing from the skill: {missing}"


def test_no_phantom_tool_calls() -> None:
    known = set(CONTROL_OPERATIONS)
    unknown: dict[str, list[str]] = {}
    for path in _markdown_files():
        found = {name for name in CALL.findall(path.read_text(encoding="utf-8")) if name not in known}
        if found:
            unknown[path.name] = sorted(found)
    assert not unknown, f"skill references tools that do not exist: {unknown}"


def test_component_references_exist_in_the_registry() -> None:
    registered = {item["component_type"] for item in default_registry().list()}
    unknown: dict[str, list[str]] = {}
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        found = sorted(
            {
                name
                for name in COMPONENT.findall(text)
                if name not in registered and name.rsplit(".", 1)[1] not in FILE_SUFFIXES
            }
        )
        if found:
            unknown[path.name] = found
    assert not unknown, f"skill references components that do not exist: {unknown}"


def test_catalogue_counts_match_the_registry() -> None:
    """Appendix B states a count per category; keep it honest."""
    registry = default_registry()
    counts: dict[str, int] = {}
    for item in registry.list():
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    labels = {
        "data": "数据",
        "feature": "特征",
        "explore": "探索",
        "visual": "可视化",
        "validation": "验证",
    }
    for category, label in labels.items():
        assert f"**{label} ({counts[category]}):**" in text, (
            f"Appendix B should say {label} ({counts[category]})"
        )


@pytest.mark.parametrize(
    "required",
    [
        "## 2. 阶段 1 — 工作区与数据准备",
        "## 3. 阶段 2 — 质量预检",
        "## 4. 阶段 3 — 窗口、分组与标签",
        "## 5. 阶段 4 — 特征",
        "## 6. 阶段 5 — 验证",
        "## 7. 阶段 6 — 执行与排错",
        "## 8. 阶段 7 — 读取结果",
        "## 9. 阶段 8 — 持久化与交接",
        "## 12. 汇报契约",
        "references/recipes.md",
        "references/troubleshooting.md",
        "references/components.md",
    ],
)
def test_stage_sections_survive_edits(required: str) -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert required in text


#: 闸门是一个小标题 + 一张自检表；正文里提到「Stage gate」（§0.1、Appendix B）不算闸门。
GATE_INTRO = "**阶段自检 —"


def _stage_gate_blocks(text: str) -> list[list[str]]:
    """每一道自检闸门：从 ``**Stage gate`` 起，到下一个二级标题为止。"""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                blocks.append(current)
            current = None
        if GATE_INTRO in line:
            current = []
        if current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _flatten(text: str) -> str:
    """Assertions must survive re-wrapping, so compare whitespace-normalised text."""
    return " ".join(text.split())


def test_every_problem_prone_stage_has_a_self_check_gate() -> None:
    """数据理解、窗口与标签、特征、验证四个阶段各有一道"命中才动手"的自检闸门。"""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    blocks = _stage_gate_blocks(text)
    assert len(blocks) >= 4, f"expected a stage gate in every problem-prone stage, found {len(blocks)}"
    for block in blocks:
        assert any(line.startswith("|") for line in block), f"gate without a self-check table: {block[0]}"


def test_stage_gates_stay_small_and_trigger_based() -> None:
    """闸门是"命中才动手"的自检表，不是逐项权衡的组件清单——后者会把任务撑爆。"""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for block in _stage_gate_blocks(text):
        rows = [line for line in block if line.startswith("|") and not line.startswith("| ---")]
        assert len(rows) - 1 <= 6, f"a stage gate grew past six branches: {block[0]}"


def test_reporting_contract_asks_for_stage_checks() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    reporting = text.split("## 12. 汇报契约", 1)[1].split("## 附录 A", 1)[0]
    assert "阶段自检" in _flatten(reporting)


def test_recon_demands_a_capability_shortlist() -> None:
    """Recon 的产物不只是环境事实，还要有"这次可能用得上的手段"清单，且不能是组件全集。"""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    recon = text.split("## 1. 侦察", 1)[1].split("## 2. 阶段 1", 1)[0]
    assert "能力清单" in _flatten(recon)
    assert "不要枚举全部 88 个组件" in _flatten(recon)


def test_entrypoint_stays_lean_and_links_every_reference() -> None:
    """The entrypoint holds decisions and routing; detail lives in references and must be reachable."""
    entry = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert len(entry.splitlines()) <= 420, "SKILL.md grew back into a handbook; move detail to references"
    for reference in sorted((SKILL_DIR / "references").glob("*.md")):
        assert f"references/{reference.name}" in entry, f"{reference.name} is not linked from SKILL.md"
