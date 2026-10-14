"""国内期货「持仓量加权指数」预警板块。

与 A 股 60 周线预警并列的独立板块：

``core``   加权指数构建 / 数据仓 / 实时加权价
``scan``   60 周线分层预警判定（突破 / 临近）
"""
from . import core, scan, varieties  # noqa: F401

__all__ = ["core", "scan", "varieties"]
