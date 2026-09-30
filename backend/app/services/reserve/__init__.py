"""储量估算业务域。

对外仍暴露 :class:`ReserveService`，路由层的旧导入路径
``from app.services.reserve import ReserveService`` 保持不变。

注意：不要导出名为 ``service`` 的实例——它会遮蔽同名的 ``service`` 子模块，
导致 ``import app.services.reserve.service`` 拿到实例而不是模块。
"""
from __future__ import annotations

from app.services.reserve.service import PublishConflict, ReserveService

__all__ = ["ReserveService", "PublishConflict"]
