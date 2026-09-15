"""``python -m fault_platform`` 的入口：直接转发到命令行解析器。

这样 ``python -m fault_platform run x.xml`` 与安装后的 ``fault-platform run x.xml``
行为完全一致，不需要额外包装脚本。
"""

from fault_platform.cli import main

main()
