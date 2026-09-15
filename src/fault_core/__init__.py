"""Standalone numerical APIs for fault prediction; no platform dependencies.

这一层是纯计算库：数据操作、窗口特征、频域/熵特征、验证与可视化规格。
它**不认识** Graph、Runtime、MCP 与前端，因此可以单独 import 到脚本或 notebook 里使用
（见 ``examples/python_api.py``）。平台层 ``fault_platform`` 依赖它，反之则不允许——
这条依赖方向是整个工程"组件可独立测试"的基础。
"""
