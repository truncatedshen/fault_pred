"""Component contracts and built-in adapters.

``base`` 定义组件契约（端口类型、参数定义、元数据、执行返回值），
``builtin`` 放 56 个内置组件实现与 :data:`~fault_platform.components.builtin.BUILTIN_COMPONENTS`
注册清单。组件只依赖 ``fault_core`` 与标准库，不认识 Graph / Runtime / MCP / UI。
"""
