"""统一登记独立业务模型，CLI、迁移和诊断不依赖 API 路由的偶然导入顺序。"""
from importlib import import_module
from .db import Base

MODEL_MODULES = ('model_routing', 'client_actions', 'media', 'auto_memory')


def registered_metadata():
    for module in MODEL_MODULES:
        import_module('.' + module, __package__)
    return Base.metadata
