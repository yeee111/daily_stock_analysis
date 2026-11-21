# -*- coding: utf-8 -*-
"""纸面风控模块 —— 交付代理 shim（2026-11-17 引擎接入后）

【单一真源迁移】经主会话批准执行引擎接入后，风控模块的生产规范副本位于
`src/services/paper_risk_control.py`（纯标准库、零 I/O、不 import src/ 其他代码，
内容与本交付物历次审查/测试验证的版本一致）。本文件改为按文件路径加载该副本
的代理（不走包导入，避免 src/services/__init__ 的重依赖），保持：
  - 干跑_风控对比.py / 自测_风控模块.py 的 `import 纸面风控模块 as rc` 不变；
  - 独立可跑、纯标准库、不依赖 DSA 服务进程的性质不变；
  - 生产与干跑/测试共用同一份代码，杜绝双副本漂移。

五项风控口径、配置键、触发键白名单、API 契约见 src/services/paper_risk_control.py
的模块 docstring 与 实现/接入说明.md（默认全关，B1 定案）。
"""

import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_仓库根 = _Path(__file__).resolve().parents[2].parent  # docs/策略优化20261116/实现 → 仓库根
_源文件 = _仓库根 / "src" / "services" / "paper_risk_control.py"
_spec = _ilu.spec_from_file_location("paper_risk_control", _源文件)
_实模块 = _ilu.module_from_spec(_spec)
# dataclasses 等按 cls.__module__ 反查 sys.modules，必须先注册再执行
_sys.modules[_spec.name] = _实模块
_spec.loader.exec_module(_实模块)


def __getattr__(name):
    return getattr(_实模块, name)


def __dir__():
    return dir(_实模块)