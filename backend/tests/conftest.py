"""测试夹具：每个用例重建内存仓库并重新引导储量域，保证相互隔离。"""
from __future__ import annotations

import importlib

import pytest

from app.store import Store

# 包的 __init__ 导出了同名实例 ``service``，会遮蔽 ``service`` 子模块，
# 这里用 importlib 明确拿到子模块本身。
service_module = importlib.import_module("app.services.reserve.service")


@pytest.fixture
def svc(monkeypatch):
    rebuilt = Store()
    # service 模块在顶部 ``from app.store import store`` 绑定了全局单例，
    # 这里替换模块属性，让整个引导/服务流程都跑在全新内存仓库上。
    monkeypatch.setattr(service_module, "store", rebuilt)
    service = service_module.ReserveService()
    service.bootstrap()
    return service
