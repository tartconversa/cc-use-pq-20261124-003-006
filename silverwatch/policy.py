"""兼容别名模块：``silverwatch.policy``，规范实现见 :mod:`silverwatch.policies`。"""
from __future__ import annotations

from silverwatch import policies as _policies
from silverwatch.contracts import AnalysisPolicy, PolicyVersion

for _name in dir(_policies):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_policies, _name)
