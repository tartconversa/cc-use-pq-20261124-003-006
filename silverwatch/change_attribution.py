"""兼容别名模块：变化归因，规范实现见 :mod:`silverwatch.attribution`。"""
from __future__ import annotations

from silverwatch.attribution import *  # noqa: F401,F403
from silverwatch import attribution as _attr

for _name in dir(_attr):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_attr, _name)
