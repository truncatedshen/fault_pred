"""Component-based fault prediction platform.

包的构成：``components``（组件契约与 56 个内置组件）、``registry``（组件目录与检索）、
``graph``（只存配置的 DAG）、``runtime``（拓扑序执行与增量指纹）、
``workspace``（产物/状态/检查点与有界观测）、``service``（网页与 MCP 共用的控制面）、
``api``（本地 HTTP/SSE）、``mcp_server``（stdio bridge）、``xml_io``（方案存取）。
数值计算全部在依赖包 ``fault_core`` 里，本包只负责组织与执行。
"""

__version__ = "0.1.0"
