"""Platform version and the compatibility range declared by every component.

每个组件元数据里都带 ``compatibility``（例如 ``fault-platform>=0.1,<1``）。
执行前的校验会调用 :func:`compatibility_error`：不满足就拒绝运行并给出人能读懂的原因。
解析是手写的轻量实现，只支持 ``>=  <=  ==  !=  >  <`` 与逗号分隔的多条件，
不引入 packaging 依赖；没有写比较符的条件按"最低版本"理解。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PLATFORM_VERSION = "0.1.0"

_REQUIREMENT = re.compile(
    r"^\s*(?P<package>[A-Za-z][A-Za-z0-9_.-]*)?\s*"
    r"(?P<operator>>=|<=|==|!=|>|<)?\s*(?P<version>\d+(?:\.\d+)*)\s*$"
)


def _as_tuple(version: str) -> tuple[int, ...]:
    """把 ``"0.1.2"`` 拆成 ``(0, 1, 2)``，便于逐段比较。"""
    return tuple(int(part) for part in version.split("."))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """按段比较版本号，短的一方补 0（因此 0.1 == 0.1.0），返回 -1/0/1。"""
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def satisfies(requirements: Iterable[str], version: str = PLATFORM_VERSION) -> bool:
    """True when ``version`` meets every clause of the component's compatibility range.

    A clause is ``[package] operator version`` (e.g. ``fault-platform>=0.1,<1``). Clauses
    without an operator are treated as a minimum version, which is the friendlier reading
    for hand-written metadata.

    任意一条不符即返回 False；写法无法解析也按 False 处理（宁可拒绝，也不放过）。
    """
    current = _as_tuple(version)
    for clause in requirements:
        for part in str(clause).split(","):
            if not part.strip():
                continue
            match = _REQUIREMENT.match(part)
            if match is None:
                return False
            operator = match.group("operator") or ">="
            target = _as_tuple(match.group("version"))
            outcome = _compare(current, target)
            if operator == ">=" and outcome < 0:
                return False
            if operator == ">" and outcome <= 0:
                return False
            if operator == "<=" and outcome > 0:
                return False
            if operator == "<" and outcome >= 0:
                return False
            if operator == "==" and outcome != 0:
                return False
            if operator == "!=" and outcome == 0:
                return False
    return True


def compatibility_error(requirements: Iterable[str], version: str = PLATFORM_VERSION) -> str | None:
    """返回"组件与当前平台不兼容"的可读原因；兼容（或无要求）时返回 None。"""
    clauses = [clause for clause in requirements if str(clause).strip()]
    if not clauses or satisfies(clauses, version):
        return None
    return (
        f"requires {', '.join(clauses)} but this platform is {version}; "
        "add a compatible implementation or upgrade the platform"
    )
