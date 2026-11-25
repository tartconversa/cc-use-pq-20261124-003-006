"""兼容别名模块：政策注册表命名空间，规范实现见 :mod:`silverwatch.policies`。"""
from __future__ import annotations

from silverwatch import policies as _policies
from silverwatch.contracts import PolicyVersion

for _name in dir(_policies):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_policies, _name)

register = _policies.register_version
register_policy = _policies.register_version
get = _policies.get_version
