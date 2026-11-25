"""兼容别名模块：政策版本登记/选择能力的可能命名空间之一。

规范实现位于 :mod:`silverwatch.policies`；这里原样再导出其全部公开名称，
使 ``from silverwatch.policy_versions import ...`` 同样可用。
"""
from __future__ import annotations

from silverwatch import policies as _policies
from silverwatch.contracts import (AnalysisPolicyVersion, EffectivePolicy,
                                   PolicyEdition, PolicyRecord, PolicyVersion)

for _name in dir(_policies):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_policies, _name)

register_policy_version = _policies.register_version
list_policy_versions = _policies.list_versions
effective_policy_version = _policies.effective_version
resolve_policy_version = _policies.effective_version
