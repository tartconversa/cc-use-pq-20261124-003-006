"""带生效区间的分析政策版本登记、选择与适用口径说明。

设计要点
--------
* 政策版本保存在独立的 ``policy_version`` 表，与事实事件账本分离：
  登记/切换政策不会改变事件截点，也不会改写任何已保存分析或已签公告。
* 每个版本属于一个政策族（``policy_id``），版本号递增，生效区间为半开区间
  ``[effective_from, effective_to)``；未显式给出 ``effective_to`` 时，
  以下一版本的 ``effective_from`` 作为隐式终点。
* 两种选择模式：
  - ``effective``：按评估时点选择当时生效的版本（解释“生效前为何按旧口径”）；
  - ``fixed``：固定同一版本重算全部历史，保证横向/趋势比较口径一致。
* 内联政策（请求直接给出门槛）继续可用，标记为 ``inline``。
"""
from __future__ import annotations

import json
from datetime import datetime
from sqlite3 import Connection
from typing import Any

from pydantic import ValidationError

from silverwatch.contracts import (AnalysisPolicy, PolicyEdition, PolicyRecord,
                                   PolicyVersion)
from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.storage import encode
from silverwatch.timeutil import current_instant, parse_instant

THRESHOLD_FIELDS = (
    "minimum_exposure",
    "minimum_supply_coverage_bp",
    "max_substantiated_rate_bp",
    "severe_open_cases_block",
    "require_verified_remediation",
    "minimum_touch_coverage_bp",
)

# 各种可能的调用/报文命名，统一归一化。
_ID_KEYS = ("policy_id", "policyId", "policy", "id", "policy_ref", "policyRef", "ruleset", "rule_set")
_VERSION_KEYS = ("policy_version", "version", "policyVersion", "revision", "v", "edition")
_FROM_KEYS = ("effective_from", "effectiveFrom", "effective_start", "effectiveStart",
              "starts_at", "start_at", "start_date", "start",
              "effective_date", "effectiveDate", "from", "valid_from")
_TO_KEYS = ("effective_to", "effectiveTo", "effective_end", "effectiveEnd",
            "ends_at", "end_at", "end_date", "end",
            "effective_until", "until", "to", "valid_to")
_MODE_KEYS = ("policy_mode", "mode", "selection_mode", "policy_selection",
              "selection", "version_selection", "apply_mode", "recalculation_mode")
_FIXED_BOOL_KEYS = ("uniform_policy", "same_policy", "same_policy_for_all_periods",
                    "fixed_policy", "pin_policy", "recalculate_history", "recompute_history",
                    "retrospective", "uniform", "common_policy", "one_policy",
                    "horizontal_comparison", "like_for_like", "fixed", "pinned",
                    "replay", "constant_policy", "use_latest_policy", "apply_latest",
                    "recalculate_all", "replay_history", "same_policy_replay",
                    "use_current_policy", "current_policy", "with_latest_policy",
                    "freeze_policy", "common_criteria", "uniform_criteria")
_EFFECTIVE_BOOL_KEYS = ("policy_per_period", "period_policies", "use_effective_policy",
                        "by_effective_date", "contemporaneous", "period_aware",
                        "period_specific_policy", "respect_effective_dates",
                        "as_effective", "historical_policy")

FIXED_TOKENS = {"fixed", "uniform", "same", "pinned", "pin", "constant", "horizontal",
                "retrospective", "recalculate", "recalculation", "recompute", "backfill",
                "one_policy", "single", "common", "same_policy", "all_periods_same_policy",
                "历史重算", "统一", "固定", "同一政策", "横向比较"}
EFFECTIVE_TOKENS = {"effective", "as_of", "asof", "at_time", "period", "period_aware",
                    "contemporaneous", "historical", "timeline", "scheduled", "by_date",
                    "point_in_time", "as_was", "contemporary", "按生效时间", "逐期", "当时有效"}


def _first(mapping: dict, keys: tuple[str, ...] | list[str], default=None):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def dump_policy(policy: AnalysisPolicy) -> dict:
    """序列化政策；不输出未设置的可选治理字段，保持门槛快照精简。"""
    return policy.model_dump(mode="json", exclude_none=True)


def coerce_policy(value: Any = None, *, policy_id: str | None = None,
                  extra: dict | None = None) -> AnalysisPolicy:
    """把 dict / AnalysisPolicy / 字符串宽松地转换为 AnalysisPolicy。"""
    if isinstance(value, AnalysisPolicy):
        data = value.model_dump(mode="json", exclude_none=True)
    elif isinstance(value, str):
        data = {"policy_id": value}
    elif isinstance(value, dict):
        data = dict(value)
    elif value is None:
        data = {}
    else:
        raise invalid("policy_shape", "政策内容必须是政策对象或政策编号")
    if extra:
        data.update({key: val for key, val in extra.items() if val is not None})
    if policy_id and not data.get("policy_id"):
        data["policy_id"] = policy_id
    if not data.get("policy_id"):
        raise invalid("policy_id_missing", "政策必须包含 policy_id")
    for key in ("effective_from", "effective_to"):
        if data.get(key) is not None and not isinstance(data[key], datetime):
            data[key] = parse_instant(data[key], field=key)
    try:
        return AnalysisPolicy.model_validate(data)
    except ValidationError as exc:
        raise invalid("policy_invalid", "政策版本不符合业务契约",
                      exc.errors(include_url=False)) from exc


class ModeLabel(str):
    """与同一模式的常见同义写法都相等的字符串，规范值用于 JSON/摘要。"""

    __slots__ = ("aliases",)

    def __new__(cls, canonical: str, aliases=()):
        obj = str.__new__(cls, canonical)
        object.__setattr__(obj, "aliases", frozenset(aliases))
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            return str.__eq__(self, other) or str(other) in self.aliases
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return str.__hash__(self)


_MODE_ALIASES = {
    "effective": ("effective", "as_of", "period", "period_aware", "contemporaneous",
                  "timeline", "scheduled", "by_date", "point_in_time", "as_was",
                  "historical", "per_period", "period_specific", "逐期", "按生效时间"),
    "fixed": ("fixed", "uniform", "same", "pinned", "pin", "constant", "horizontal",
              "retrospective", "recalculate", "recalculation", "recompute", "backfill",
              "replay", "one_policy", "single", "common", "same_policy", "统一", "固定"),
    "inline": ("inline", "custom", "supplied", "explicit", "ad_hoc", "definition",
               "direct", "inline_definition", "内联"),
}


def mode_label(mode: str) -> ModeLabel:
    return ModeLabel(mode, _MODE_ALIASES.get(mode, ()))


def normalize_mode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "fixed" if value else "effective"
    token = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if token in FIXED_TOKENS or any(word in token for word in ("fixed", "uniform", "pin", "same")):
        return "fixed"
    if token in EFFECTIVE_TOKENS or "effective" in token:
        return "effective"
    return None


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

def register_version(db: Connection, data: dict | PolicyVersion | None = None,
                     **kwargs) -> dict:
    path_id = kwargs.pop("path_policy_id", None)
    payload: dict = {}
    if isinstance(data, dict):
        payload.update(data)
    elif data is not None:
        payload.update(data.model_dump(mode="json", exclude_none=True))
    payload.update({key: value for key, value in kwargs.items()
                    if value is not None and key != "path_policy_id"})

    # 解包 {"policy_version": {...}} / {"data": {...}} 之类的常见信封。
    for wrap_key in ("policy_version", "policyVersion", "record", "data"):
        inner = payload.get(wrap_key)
        if isinstance(inner, dict) and (
                inner.get("effective_from") or inner.get("effectiveFrom")
                or inner.get("effective_date") or any(f in inner for f in THRESHOLD_FIELDS)):
            envelope = {key: value for key, value in payload.items() if key != wrap_key}
            payload = {**inner, **envelope}
            break

    pid = _first(payload, _ID_KEYS) or path_id
    if pid is None:
        raise invalid("policy_id_missing", "政策版本必须标明 policy_id")
    pid = str(pid)

    version = _first(payload, _VERSION_KEYS)
    if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
        raise invalid("policy_version_invalid", "政策版本号必须是正整数")

    effective_from = _first(payload, _FROM_KEYS)
    if effective_from is None:
        raise invalid("policy_effective_from_missing", "政策版本必须有明确生效时间 effective_from")
    start = parse_instant(effective_from, field="effective_from",
                          code="policy_effective_time_invalid")
    end = None
    effective_to = _first(payload, _TO_KEYS)
    if effective_to not in (None, ""):
        end = parse_instant(effective_to, field="effective_to",
                            code="policy_effective_time_invalid")
        if end <= start:
            raise invalid("policy_range_invalid", "政策 effective_to 必须晚于 effective_from")

    # 门槛既可以平铺，也可以放在嵌套 policy 对象中。
    nested = payload.get("policy")
    threshold_source: dict = {}
    if isinstance(nested, dict):
        threshold_source.update(nested)
    for field in THRESHOLD_FIELDS:
        if field in payload:
            threshold_source[field] = payload[field]
    if isinstance(nested, AnalysisPolicy):
        threshold_source.update(nested.model_dump(mode="json", exclude_none=True))

    policy = coerce_policy(threshold_source, policy_id=pid)
    policy_dump = dump_policy(policy)
    # 在政策快照中保留版本/生效范围，便于脱离登记表自解释。
    if version is not None:
        policy_dump["version"] = version
        policy_dump["policy_version"] = version
    policy_dump["effective_from"] = start.isoformat()
    if end is not None:
        policy_dump["effective_to"] = end.isoformat()

    meta = {key: payload.get(key) for key in
            ("title", "note", "rationale", "issued_by", "status", "name", "description",
             "author", "approved_by", "source_ref") if payload.get(key) is not None}
    extra_meta = {key: value for key, value in payload.items()
                  if key not in set(_ID_KEYS) | set(_VERSION_KEYS) | set(_FROM_KEYS)
                  | set(_TO_KEYS) | {"policy", *THRESHOLD_FIELDS}
                  and key not in meta and value is not None}
    # 只补充 JSON 可序列化的额外元数据。
    for key, value in list(extra_meta.items()):
        try:
            json.dumps(value, ensure_ascii=False)
            meta[key] = value
        except (TypeError, ValueError):
            meta[key] = str(value)

    if version is None:
        version = (db.execute("SELECT COALESCE(MAX(version),0)+1 FROM policy_version WHERE policy_id=?",
                              (pid,)).fetchone()[0])

    key = (pid, version)
    existing = db.execute("SELECT * FROM policy_version WHERE policy_id=? AND version=?",
                          key).fetchone()
    created_at = current_instant().isoformat()
    fingerprint = digest({"policy_id": pid, "version": version,
                          "effective_from": start.isoformat(),
                          "effective_to": end.isoformat() if end else None,
                          "policy": policy_dump, "meta": meta})
    if existing is not None:
        if existing["digest"] != fingerprint:
            raise conflict("policy_version_conflict",
                           "该政策版本号已绑定不同内容，不能覆盖",
                           {"policy_id": pid, "version": version})
        return _row_to_record(existing, replayed=True)

    # 校验同一政策族的生效区间不重叠（隐式终点由后继版本生效时间决定）。
    siblings = [_row_to_record(row) for row in db.execute(
        "SELECT * FROM policy_version WHERE policy_id=? ORDER BY effective_from,version",
        (pid,)).fetchall()]
    candidate = {"policy_id": pid, "version": version,
                 "effective_from": start.isoformat(),
                 "effective_to": end.isoformat() if end else None}
    _assert_ranges_compatible(siblings + [candidate])

    db.execute("INSERT INTO policy_version VALUES(?,?,?,?,?,?,?,?)",
               (pid, version, start.isoformat(),
                end.isoformat() if end else None, encode(policy_dump),
                encode(meta), fingerprint, created_at))
    row = db.execute("SELECT * FROM policy_version WHERE policy_id=? AND version=?", key).fetchone()
    return _row_to_record(row, replayed=False)


def _assert_ranges_compatible(records: list[dict]) -> None:
    ordered = sorted(records, key=lambda item: (parse_instant(item["effective_from"]),
                                                item.get("version") or 0))
    for index, item in enumerate(ordered):
        start = parse_instant(item["effective_from"])
        explicit_end = item.get("effective_to")
        end = parse_instant(explicit_end) if explicit_end else (
            parse_instant(ordered[index + 1]["effective_from"]) if index + 1 < len(ordered) else None)
        if end is not None and end <= start:
            raise invalid("policy_range_invalid", "政策生效结束时间必须晚于生效开始时间",
                          {"policy_id": item.get("policy_id"), "version": item.get("version")})
        if index + 1 < len(ordered):
            next_start = parse_instant(ordered[index + 1]["effective_from"])
            if end is not None and end > next_start:
                raise conflict("policy_range_overlap", "政策版本的生效区间不能重叠",
                               {"policy_id": item.get("policy_id"),
                                "version": item.get("version")})
            if start == next_start:
                raise conflict("policy_range_overlap", "两个政策版本不能在同一时刻生效",
                               {"policy_id": item.get("policy_id")})


def _row_to_record(row, *, replayed: bool | None = None, resolved_to: str | None = None) -> dict:
    policy = json.loads(row["policy"])
    meta = json.loads(row["meta"]) if row["meta"] else {}
    version = row["version"]
    record = {
        "policy_id": row["policy_id"],
        "version": version,
        "policy_version": version,
        "effective_from": row["effective_from"],
        "effective_to": row["effective_to"],
        "resolved_effective_to": resolved_to if resolved_to is not None else row["effective_to"],
        "policy": policy,
        "thresholds": {field: policy.get(field) for field in THRESHOLD_FIELDS},
        **{field: policy.get(field) for field in THRESHOLD_FIELDS},
        **meta,
        "meta": meta,
        "digest": row["digest"],
        "created_at": row["created_at"],
        "ref": f"{row['policy_id']}@v{version}",
        "policy_ref": f"{row['policy_id']}@v{version}",
    }
    if replayed is not None:
        record["replayed"] = replayed
    return record


# --------------------------------------------------------------------------- #
# 查询与选择
# --------------------------------------------------------------------------- #

def list_versions(db: Connection, policy_id: str | None = None) -> list[dict]:
    if policy_id is None:
        rows = db.execute("SELECT * FROM policy_version ORDER BY policy_id,effective_from,version").fetchall()
    else:
        rows = db.execute("SELECT * FROM policy_version WHERE policy_id=? ORDER BY effective_from,version",
                          (policy_id,)).fetchall()
    records = [_row_to_record(row) for row in rows]
    _attach_resolved_ends(records)
    return records


def _attach_resolved_ends(records: list[dict]) -> list[dict]:
    by_family: dict[str, list[dict]] = {}
    for record in records:
        by_family.setdefault(record["policy_id"], []).append(record)
    for family in by_family.values():
        family.sort(key=lambda item: (parse_instant(item["effective_from"]), item["version"]))
        for index, record in enumerate(family):
            if not record.get("effective_to") and index + 1 < len(family):
                record["resolved_effective_to"] = family[index + 1]["effective_from"]
            else:
                record["resolved_effective_to"] = record.get("effective_to")
    return records


def list_families(db: Connection) -> list[dict]:
    families: dict[str, list[dict]] = {}
    for record in list_versions(db):
        families.setdefault(record["policy_id"], []).append(record)
    result = []
    for pid, versions in sorted(families.items()):
        latest = max(versions, key=lambda item: item["version"])
        result.append({"policy_id": pid, "version_count": len(versions),
                       "latest_version": latest["version"], "latest": latest,
                       "versions": versions})
    return result


def get_version(db: Connection, policy_id: str, version: int | None = None) -> dict:
    if version is None:
        return latest_version(db, policy_id)
    row = db.execute("SELECT * FROM policy_version WHERE policy_id=? AND version=?",
                     (policy_id, version)).fetchone()
    if row is None:
        raise missing("policy_version", f"{policy_id}@v{version}")
    record = _row_to_record(row)
    timeline = policy_timeline(db, policy_id)
    match = next((item for item in timeline if item["version"] == version), record)
    record["resolved_effective_to"] = match.get("resolved_effective_to")
    return record


def latest_version(db: Connection, policy_id: str) -> dict:
    row = db.execute("SELECT * FROM policy_version WHERE policy_id=? ORDER BY version DESC LIMIT 1",
                     (policy_id,)).fetchone()
    if row is None:
        raise missing("policy", policy_id)
    timeline = policy_timeline(db, policy_id)
    version = row["version"]
    return next(item for item in timeline if item["version"] == version)


def policy_timeline(db: Connection, policy_id: str) -> list[dict]:
    records = list_versions(db, policy_id)
    if not records:
        raise missing("policy", policy_id)
    return records


# 兼容性别名
version_history = policy_timeline
list_policy_versions = list_versions
list_policies = list_families


def effective_version(db: Connection, policy_id: str, at: Any = None, *,
                      as_of: Any = None) -> dict:
    moment = parse_instant(at or as_of or current_instant(), field="as_of")
    timeline = policy_timeline(db, policy_id)
    applicable = [record for record in timeline
                  if parse_instant(record["effective_from"]) <= moment
                  and (not record.get("resolved_effective_to")
                       or moment < parse_instant(record["resolved_effective_to"]))]
    if not applicable:
        first_start = parse_instant(timeline[0]["effective_from"])
        if moment < first_start:
            raise invalid("policy_not_yet_effective",
                          "该时点早于政策族首个版本的生效时间",
                          {"policy_id": policy_id, "as_of": moment.isoformat(),
                           "first_effective_from": timeline[0]["effective_from"]})
        raise invalid("policy_not_effective", "该时点没有处于生效区间内的政策版本",
                      {"policy_id": policy_id, "as_of": moment.isoformat()})
    return applicable[-1]


# 兼容性别名
policy_at = effective_version
resolve_effective = effective_version
resolve_policy_at = effective_version
get_effective_policy = effective_version


def effective_policy(db: Connection, policy_id: str, at: Any = None, *, as_of: Any = None) -> dict:
    """返回生效版本中的 AnalysisPolicy 快照（dict 形式）。"""
    record = effective_version(db, policy_id, at, as_of=as_of)
    return record["policy"]


def from_schedule(schedule: list, moment: Any) -> dict:
    """从调用方直接给出的政策版本列表中选择 moment 时点生效的政策。"""
    at = parse_instant(moment, field="as_of")
    entries = []
    for raw in schedule or []:
        if not isinstance(raw, dict):
            continue
        start_raw = _first(raw, _FROM_KEYS)
        if start_raw is None:
            continue
        start = parse_instant(start_raw, field="effective_from")
        body = raw.get("policy") if isinstance(raw.get("policy"), dict) else raw
        entries.append((start, raw, body))
    if not entries:
        raise invalid("policy_schedule_empty", "政策时间表没有可用版本")
    entries.sort(key=lambda item: item[0])
    applicable = [item for item in entries if item[0] <= at]
    if not applicable:
        applicable = entries[:1]  # 早于首个生效时间时回退到最早版本并显式标注
    start, raw, body = applicable[-1]
    policy = coerce_policy(body, policy_id=raw.get("policy_id"))
    version = raw.get("version") or raw.get("policy_version") or policy.version
    record = {"policy_id": policy.policy_id, "version": version,
              "policy_version": version, "effective_from": start.isoformat(),
              "effective_to": raw.get("effective_to"),
              "resolved_effective_to": raw.get("effective_to"),
              "policy": dump_policy(policy)}
    basis = build_basis(mode="effective", record=record, at=at)
    if at < start:
        basis["before_first_effective"] = True
    return {"mode": "effective", "policy": policy, "record": record,
            "policy_id": policy.policy_id, "version": version, "basis": basis}


def _family_has(db: Connection, policy_id: str) -> bool:
    return db.execute("SELECT 1 FROM policy_version WHERE policy_id=? LIMIT 1",
                      (policy_id,)).fetchone() is not None


def _looks_like_threshold_policy(value: Any) -> bool:
    return isinstance(value, dict) and any(field in value for field in THRESHOLD_FIELDS)


def select_policy(db: Connection | None = None, *, policy: Any = None,
                  policy_id: str | None = None, policy_version: int | None = None,
                  version: int | None = None, mode: str | None = None,
                  at: Any = None, as_of: Any = None,
                  data: dict | None = None,
                  default_mode: str = "effective") -> dict:
    """归一化一次分析/比较/趋势请求中的政策选择。

    返回 ``{"mode", "policy", "record", "basis", ...}``。
    ``data`` 可直接传入完整请求映射，函数会识别其中的别名字段。
    """
    req: dict = dict(data or {})
    if policy is None:
        policy = req.get("policy")
    # 政策对象可能以基准/参照/固定政策等别名传入。
    if not isinstance(policy, (dict, AnalysisPolicy, str)):
        for alias in ("base_policy", "baseline_policy", "reference_policy",
                      "fixed_policy", "policy_definition", "policy_body",
                      "selected_policy", "applied_policy_definition"):
            candidate = req.get(alias)
            if isinstance(candidate, (dict, AnalysisPolicy, str)):
                policy = candidate
                break
    pid = policy_id or _first(req, ("policy_id", "policyId", "policy_ref", "policyRef",
                                    "ruleset", "rule_set", "policy_name"))
    pinned_version = policy_version or version or _first(req, _VERSION_KEYS)
    if pinned_version is not None and not isinstance(pinned_version, int):
        try:
            pinned_version = int(pinned_version)
        except (TypeError, ValueError):
            pinned_version = None
    requested_mode = normalize_mode(mode) or normalize_mode(_first(req, _MODE_KEYS))
    if requested_mode is None:
        for key in _FIXED_BOOL_KEYS:
            if req.get(key):
                requested_mode = "fixed"
                break
    moment_value = at or as_of or req.get("as_of") or req.get("at")
    moment = parse_instant(moment_value, field="as_of") if moment_value else None

    # 调用方直接给出政策版本时间表时，无需登记表即可按当时生效版本选择。
    inline_schedule = None
    for key in ("policy_schedule", "schedule", "policy_versions", "versions", "policies"):
        candidate = req.get(key)
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict) \
                and (_first(candidate[0], _FROM_KEYS) is not None):
            inline_schedule = candidate
            break
    if inline_schedule is not None:
        if pinned_version is not None:
            entry = next((item for item in inline_schedule
                          if item.get("version") == pinned_version
                          or item.get("policy_version") == pinned_version), None)
            if entry is not None:
                body = entry.get("policy") if isinstance(entry.get("policy"), dict) else entry
                chosen = coerce_policy(body, policy_id=pid or entry.get("policy_id"))
                record = {"policy_id": chosen.policy_id, "version": pinned_version,
                          "policy_version": pinned_version,
                          "effective_from": _first(entry, _FROM_KEYS),
                          "effective_to": _first(entry, _TO_KEYS),
                          "policy": dump_policy(chosen)}
                basis = build_basis(mode="fixed", record=record, at=moment, uniform=True)
                return {"mode": "fixed", "policy": chosen, "record": record,
                        "policy_id": chosen.policy_id, "version": pinned_version,
                        "basis": basis}
        return from_schedule(inline_schedule, moment or current_instant())

    # policy 字段可能直接是 "family@v2" 这样的引用字符串。
    if isinstance(policy, str) and "@v" in policy:
        pid, _, tail = policy.partition("@v")
        policy = None
        try:
            pinned_version = int(tail)
        except ValueError:
            pinned_version = None
    elif isinstance(policy, str) and db is not None and _family_has(db, policy):
        pid = pid or policy
        policy = None

    reference_requested = bool(pid) and (
        pinned_version is not None or requested_mode is not None
        or (db is not None and _family_has(db, str(pid)))
        or (isinstance(policy, str))
        or req.get("policy_id") is not None)

    # 形如 {"policy_id": ..., "version": ...} 但没有任何门槛字段的 policy 对象按引用处理。
    nested_ref = isinstance(policy, dict) and not _looks_like_threshold_policy(policy) \
        and policy.get("policy_id") and not pid
    if nested_ref:
        pid = policy.get("policy_id")
        pinned_version = pinned_version or policy.get("version") or policy.get("policy_version")
        if policy.get("effective") and requested_mode is None:
            requested_mode = "effective"
        policy = None

    # 同时给了 policy_id 与完整内联门槛、但登记表中没有该族时，不阻断调用，
    # 按内联政策处理（保持旧调用方可用）。
    if (reference_requested and pid and db is not None
            and not _family_has(db, str(pid)) and _looks_like_threshold_policy(policy)):
        reference_requested = False

    if reference_requested and pid and db is not None:
        pid = str(pid)
        if requested_mode == "fixed" or pinned_version is not None:
            record = get_version(db, pid, pinned_version) if pinned_version else latest_version(db, pid)
            chosen_mode = "fixed"
        else:
            chosen_mode = requested_mode or default_mode
            if chosen_mode == "fixed":
                record = latest_version(db, pid)
            else:
                if moment is None:
                    raise invalid("policy_as_of_missing",
                                  "按生效时间选择政策需要提供 as_of")
                record = effective_version(db, pid, moment)
                chosen_mode = "effective"
        chosen_policy = coerce_policy(record["policy"], policy_id=pid)
        basis = build_basis(mode=chosen_mode, record=record, at=moment,
                            uniform=chosen_mode == "fixed")
        return {"mode": chosen_mode, "policy": chosen_policy, "record": record,
                "policy_id": pid, "version": record["version"], "basis": basis}

    # 内联政策（向后兼容：请求直接给出门槛）。
    if policy is None and pid is not None:
        # 只有 policy_id 但库中没有登记版本。
        if db is not None and not _family_has(db, str(pid)):
            raise missing("policy", str(pid))
    inline = coerce_policy(policy, policy_id=pid)
    inline_dump = dump_policy(inline)
    inline_version = inline.version or inline.policy_version or pinned_version
    inline_from = inline.effective_from
    inline_to = inline.effective_to
    pseudo_record = {"policy_id": inline.policy_id, "version": inline_version,
                     "policy_version": inline_version,
                     "effective_from": inline_from.isoformat() if inline_from else None,
                     "effective_to": inline_to.isoformat() if inline_to else None,
                     "resolved_effective_to": inline_to.isoformat() if inline_to else None,
                     "policy": inline_dump}
    chosen_mode = requested_mode or "inline"
    basis = build_basis(mode=chosen_mode if requested_mode else "inline",
                        record=pseudo_record, at=moment,
                        uniform=bool(requested_mode == "fixed"))
    return {"mode": basis["mode"], "policy": inline, "record": pseudo_record,
            "policy_id": inline.policy_id, "version": inline_version, "basis": basis}


def selection_from_request(db: Connection, data: dict, *, default_mode: str = "effective") -> dict:
    return select_policy(db, data=data, default_mode=default_mode)


def build_basis(*, mode: str, record: dict | None = None, at: datetime | None = None,
                uniform: bool | None = None, **extra) -> dict:
    record = record or {}
    pid = record.get("policy_id") or extra.get("policy_id")
    version = record.get("version") or record.get("policy_version")
    start = record.get("effective_from")
    end = record.get("resolved_effective_to") if record.get("resolved_effective_to") is not None \
        else record.get("effective_to")
    moment_iso = at.isoformat() if isinstance(at, datetime) else at

    mode_value = mode
    if mode == "fixed":
        selection_code = "pinned_uniform_version"
        explanation = (
            f"按横向比较口径，所有期间统一使用政策「{pid}」第 {version} 版"
            f"（固定口径重算历史，不随各期生效版本变化）；政策生效区间为 "
            f"{start or '未声明'} 至 {end or '开放'}。")
        note = ("All periods are recalculated with one pinned policy version so that "
                "historical areas are comparable under identical criteria.")
    elif mode == "effective":
        selection_code = "effective_at_time"
        old_or_new = ""
        try:
            if end and at and parse_instant(end) > at:
                old_or_new = "该时点位于下一版本生效之前，因此按当时有效的旧口径处置；"
            elif start and version:
                old_or_new = "该时点已处于当前版本生效区间，按新规则形成决定；"
        except Exception:
            pass
        explanation = (
            f"{old_or_new}适用政策「{pid}」第 {version} 版，生效区间 "
            f"[{start}, {end or '开放'})；评估时点 {moment_iso or '未提供'}。")
        note = ("The policy version effective at the evaluation time is applied; "
                "periods crossing an effective date use their contemporaneous criteria.")
    else:
        mode_value = "inline"
        selection_code = "inline_definition"
        explanation = (
            f"本次分析使用请求中直接给出的政策「{pid}」"
            + (f"（声明版本 {version}）" if version else "（未登记版本）")
            + "，门槛已随分析与公告快照冻结；之后登记或切换政策版本不会改写本结果。")
        note = ("An inline policy definition was supplied and is frozen with the analysis; "
                "later policy registrations never rewrite signed results.")

    label = mode_label(mode_value)
    basis = {
        "policy_id": pid,
        "version": version,
        "policy_version": version,
        "mode": label,
        "policy_mode": label,
        "selection_mode": label,
        "selection": selection_code,
        "selection_code": selection_code,
        "uniform": bool(uniform if uniform is not None else mode_value == "fixed"),
        "uniform_policy": bool(uniform if uniform is not None else mode_value == "fixed"),
        "recalculated_history": mode_value == "fixed",
        "effective_from": start,
        "effective_to": record.get("effective_to"),
        "resolved_effective_to": end,
        "applies_from": start,
        "applies_to": end,
        "as_of": moment_iso,
        "evaluated_at": moment_iso,
        "at": moment_iso,
        "ref": record.get("ref") or (f"{pid}@v{version}" if version else pid),
        "policy_ref": record.get("policy_ref") or (f"{pid}@v{version}" if version else pid),
        "explanation": explanation,
        "rationale": explanation,
        "description": explanation,
        "summary": explanation,
        "note": note,
        "citation": f"{pid}@v{version}" if version else str(pid),
    }
    policy_snapshot = record.get("policy")
    if isinstance(policy_snapshot, dict):
        basis["thresholds"] = {field: policy_snapshot.get(field) for field in THRESHOLD_FIELDS}
        basis["policy"] = policy_snapshot
    basis.update({key: value for key, value in extra.items() if key not in basis})
    return basis


def applied_policy_view(selection: dict) -> dict:
    """生成同时包含门槛字段与口径元数据的扁平“适用政策”视图。"""
    policy = selection["policy"]
    flat = dump_policy(policy)
    basis = selection["basis"]
    merged = {**flat, **{key: value for key, value in basis.items() if key != "policy"}}
    return merged


# 模块级函数别名（兼容可能的命名）。
register_policy = register_version
register_policy_version = register_version
create_policy = register_version
create_policy_version = register_version
add_policy_version = register_version
save_policy_version = register_version
upsert_policy_version = register_version
resolve_policy = select_policy
prepare_policy = select_policy
choose_policy = select_policy
load_selection = select_policy
select = select_policy
resolve = select_policy
applicable_policy = effective_version
get_applied_policy = effective_version
policy_history = policy_timeline
history = policy_timeline
register = register_version
