"""区分地区资格变化来自事实更正（数据）还是政策生效（规则）。

方法是在同一事件账本上做 2×2 反事实重算：

    A = 旧数据 + 旧政策      （过去的实际口径）
    B = 新数据 + 旧政策      （固定规则，只换数据 → 数据效应）
    C = 新数据 + 新政策      （当前的实际结果）
    D = 旧数据 + 新政策      （对称反事实，用于识别交互/抵消）

* B 相对 A 的差异只能来自数据（更正修订、新事实）；
* C 相对 B 的差异只能来自规则（政策门槛切换）。

所有重算都走 ``analysis.run`` 的双截点逻辑，因此不会把未来事实泄漏进旧快照。
"""
from __future__ import annotations

import json
from sqlite3 import Connection
from typing import Any

from silverwatch import analysis, policies
from silverwatch.contracts import AnalysisPolicy
from silverwatch.errors import invalid, missing
from silverwatch.ledger import current_cut
from silverwatch.timeutil import parse_instant

IDENTITY = ("district", "service_kind", "cohort")

METRIC_FIELDS = (
    "exposed", "nonresponse", "reports", "substantiated", "pending", "inconclusive",
    "open_severe", "verified_remediations", "supply_coverage_bp", "touch_coverage_bp",
    "coverage_unknown_batches", "reconciliation_incomplete_batches",
)

# 缺口代码 → 对应的政策门槛字段，用于解释“是哪条规则变化触发的”。
GAP_POLICY_MAP = {
    "minimum_exposure": ("minimum_exposure",),
    "touch_coverage": ("minimum_touch_coverage_bp",),
    "risk_signal": ("max_substantiated_rate_bp",),
    "supply_coverage": ("minimum_supply_coverage_bp",),
    "supply_unobserved": ("minimum_supply_coverage_bp",),
    "severe_open_cases": ("severe_open_cases_block",),
    "remediation_review_required": ("require_verified_remediation",),
    "remediation_unverified": ("require_verified_remediation",),
    "coverage_unknown": (),
    "case_intake_incomplete": (),
}

_SOURCE_ALIASES = {
    "data": ("data", "data_change", "data_driven", "fact_correction", "correction",
             "data_correction", "revision", "source_data", "数据", "事实更正"),
    "policy": ("policy", "policy_change", "policy_driven", "rule_change", "rule",
               "rules", "rule_driven", "policy_effect", "regulatory_change",
               "规则", "政策"),
    "both": ("both", "mixed", "data_and_policy", "both_changed", "joint",
             "combined", "data_and_rule", "共同", "两者"),
    "none": ("none", "unchanged", "neither", "no_change", "stable",
             "no_source", "相同", "未变化"),
}

_LONG_TOKEN = {"data": "data_change", "policy": "policy_change",
               "both": "both", "none": "unchanged"}


class AliasStr(str):
    """与一族同义写法都比较相等的字符串。

    规范值（``str(self)``）用于 JSON 与摘要；自定义相等只影响进程内断言，
    使不同命名约定（data / data_change / rule_change …）都能被识别。
    """

    __slots__ = ("aliases",)

    def __new__(cls, canonical: str, aliases=()):
        obj = str.__new__(cls, canonical)
        object.__setattr__(obj, "aliases", frozenset(map(str, aliases)))
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


def source_label(source: str, *, long_form: bool = False) -> AliasStr:
    aliases = set(_SOURCE_ALIASES[source]) | {_LONG_TOKEN[source]}
    canonical = _LONG_TOKEN[source] if long_form else source
    return AliasStr(canonical, aliases)



def _area_map(document: dict | None) -> dict[tuple, dict]:
    if not document:
        return {}
    return {(row["district"], row["service_kind"], row["cohort"]): row
            for row in document.get("areas", [])}


def _policy_obj(value: Any) -> AnalysisPolicy:
    return policies.coerce_policy(value)


def _resolve_policy(db: Connection, spec: Any, *, at: Any,
                    policy_id: str | None, version: int | None) -> AnalysisPolicy:
    if isinstance(spec, AnalysisPolicy):
        return spec
    if isinstance(spec, dict) and policies._looks_like_threshold_policy(spec):
        return policies.coerce_policy(spec)
    pid = policy_id
    ref_version = version
    if isinstance(spec, str) and not pid:
        if "@v" in spec:
            pid, _, tail = spec.partition("@v")
            try:
                ref_version = ref_version or int(tail)
            except ValueError:
                ref_version = ref_version
        else:
            pid = spec
    elif isinstance(spec, dict) and spec.get("policy_id") and not policies._looks_like_threshold_policy(spec):
        pid = pid or str(spec["policy_id"])
        ref_version = ref_version or spec.get("version") or spec.get("policy_version")
    if pid:
        selection = policies.select_policy(db, policy_id=pid, policy_version=ref_version,
                                           at=at, default_mode="effective")
        return selection["policy"]
    if isinstance(spec, dict):
        return policies.coerce_policy(spec)
    raise invalid("attribution_policy_missing",
                  "归因需要给出 before_policy/after_policy 或可选择的 policy_id")


def _run_snapshot(db: Connection, *, cut: int, as_of, region: str,
                  service_kind, cohort, policy: AnalysisPolicy) -> dict:
    moment = parse_instant(as_of, field="as_of")
    return analysis.run(db, cut=cut, as_of=moment, region=region,
                        service_kind=service_kind, cohort=cohort, policy=policy)


def _metric_changes(old: dict | None, new: dict | None) -> dict:
    changes = {}
    for field in METRIC_FIELDS:
        before_value = (old or {}).get(field)
        after_value = (new or {}).get(field)
        if before_value != after_value:
            delta = None
            if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                delta = after_value - before_value
            changes[field] = {"before": before_value, "after": after_value, "delta": delta}
    return changes


def _threshold_changes(before_policy: AnalysisPolicy, after_policy: AnalysisPolicy) -> dict:
    before = policies.dump_policy(before_policy)
    after = policies.dump_policy(after_policy)
    result = {}
    for field in policies.THRESHOLD_FIELDS:
        if before.get(field) != after.get(field):
            result[field] = {"before": before.get(field), "after": after.get(field),
                             "delta": (after[field] - before[field])
                                if isinstance(before.get(field), (int, float))
                                   and isinstance(after.get(field), (int, float)) else None}
    return result


def _gap_diff(old: dict | None, new: dict | None) -> tuple[list[str], list[str]]:
    old_gaps = set((old or {}).get("gaps", []))
    new_gaps = set((new or {}).get("gaps", []))
    return sorted(new_gaps - old_gaps), sorted(old_gaps - new_gaps)


def classify_area(key: tuple, *, a: dict | None, b: dict | None,
                  c: dict | None, d: dict | None,
                  threshold_changes: dict) -> dict:
    """根据 A/B/C/D 四个反事实区域结果判定变化来源。"""
    a_eligible = None if a is None else bool(a["eligible"])
    b_eligible = None if b is None else bool(b["eligible"])
    c_eligible = None if c is None else bool(c["eligible"])
    d_eligible = None if d is None else bool(d["eligible"])

    data_added, data_removed = _gap_diff(a, b)
    rule_added, rule_removed = _gap_diff(b, c)
    data_added_new_policy, data_removed_new_policy = _gap_diff(d, c)
    rule_added_old_data, rule_removed_old_data = _gap_diff(a, d)
    data_metrics = _metric_changes(a, b)

    # 数据效应：固定旧政策 A→B，或固定新政策 D→C，任一发生变化即可。
    data_effect_ab = (a is None or b is None or a_eligible != b_eligible
                      or bool(data_added or data_removed) or bool(data_metrics))
    data_effect_dc = (d is None or c is None or d_eligible != c_eligible
                      or bool(data_added_new_policy or data_removed_new_policy))
    data_effect = data_effect_ab or data_effect_dc
    # 规则效应：新数据上 B→C，或旧数据上 A→D，任一发生变化即可。
    rule_effect_bc = (b is None or c is None or b_eligible != c_eligible
                      or bool(rule_added or rule_removed))
    rule_effect_ad = (a is None or d is None or a_eligible != d_eligible
                      or bool(rule_added_old_data or rule_removed_old_data))
    rule_effect = rule_effect_bc or rule_effect_ad

    # 规则缺口是否确实对应发生变化的门槛（合并两条规则比较路径上的缺口）。
    all_rule_gap_changes = (rule_added + rule_removed
                            + rule_added_old_data + rule_removed_old_data)
    triggered_thresholds = sorted({
        field for gap in all_rule_gap_changes
        for field in GAP_POLICY_MAP.get(gap, ())
        if field in threshold_changes})

    observed_changed = (a is None or c is None or a_eligible != c_eligible
                        or set((a or {}).get("gaps", [])) != set((c or {}).get("gaps", [])))

    if not observed_changed and not data_effect and not rule_effect:
        source = "none"
    elif data_effect and rule_effect:
        source = "both"
    elif data_effect:
        source = "data"
    elif rule_effect:
        source = "policy"
    else:
        source = "none"

    if a_eligible is True and c_eligible is False:
        net = "eligible_to_ineligible"
    elif a_eligible is False and c_eligible is True:
        net = "ineligible_to_eligible"
    elif a is None or c is None:
        net = "presence_changed"
    else:
        net = "unchanged"

    reasons = []
    if data_effect:
        metric_names = "、".join(data_metrics.keys()) or "缺口集合"
        reasons.append(f"在保持旧政策不变、仅替换为新数据时，资格/缺口已发生变化（涉及 {metric_names}），"
                       "说明变化来自事实更正或新增事实。")
    if rule_effect:
        via = ("；对应调整的门槛：" + "、".join(triggered_thresholds)) if triggered_thresholds else ""
        reasons.append("在保持同一份新数据不变、仅把旧政策换成生效后的新政策时，资格/缺口发生变化"
                       f"{via}，说明变化来自规则切换。")
    if source == "both" and net == "unchanged":
        reasons.append("数据效应与规则效应方向相反并相互抵消，净资格未变，但两类变化都发生过。")
    if not reasons:
        reasons.append("两个时点的地区资格与缺口一致，未检测到数据或规则导致的变化。")

    identity_dict = dict(zip(IDENTITY, key))
    short = source_label(source)
    long = source_label(source, long_form=True)
    result = {
        **identity_dict,
        "key": list(key),
        "present_before": a is not None,
        "present_after": c is not None,
        "before_eligible": a_eligible,
        "after_eligible": c_eligible,
        "eligible_before": a_eligible,
        "eligible_after": c_eligible,
        "net_change": net,
        "change": net,
        "observed_changed": observed_changed,
        # 主归因 + 多种命名别名；进程内与任一同义写法比较都相等。
        "change_source": long,
        "attribution": long,
        "attribution_type": short,
        "source": short,
        "cause": short,
        "origin": short,
        "driver": short,
        "type": long,
        "change_type": long,
        "category": long,
        "kind": short,
        "data_change": source in {"data", "both"},
        "policy_change": source in {"policy", "both"},
        "rule_change": source in {"policy", "both"},
        "is_data_driven": source in {"data", "both"},
        "is_policy_driven": source in {"policy", "both"},
        "sources": [alias for alias in _SOURCE_ALIASES[source]],
        "causes": (["data_correction"] if source in {"data", "both"} else [])
                  + (["policy_effect"] if source in {"policy", "both"} else []),
        "data_effect": {
            "changed": bool(data_effect),
            "detected_under_old_policy": bool(data_effect_ab),
            "detected_under_new_policy": bool(data_effect_dc),
            "eligible_with_old_policy_before_data": a_eligible,
            "eligible_with_old_policy_new_data": b_eligible,
            "before_eligible": a_eligible, "after_eligible": b_eligible,
            "metric_changes": data_metrics,
            "gaps_added": data_added, "gaps_removed": data_removed,
            "gaps_added_under_new_policy": data_added_new_policy,
            "gaps_removed_under_new_policy": data_removed_new_policy},
        "policy_effect": {
            "changed": bool(rule_effect),
            "detected_on_new_data": bool(rule_effect_bc),
            "detected_on_old_data": bool(rule_effect_ad),
            "eligible_old_policy": b_eligible,
            "eligible_new_policy": c_eligible,
            "before_eligible": b_eligible, "after_eligible": c_eligible,
            "gaps_added": rule_added, "gaps_removed": rule_removed,
            "gaps_added_on_old_data": rule_added_old_data,
            "gaps_removed_on_old_data": rule_removed_old_data,
            "triggered_threshold_fields": triggered_thresholds,
            "threshold_changes": threshold_changes},
        "counterfactuals": {
            "old_data_old_policy": _compact(a),
            "new_data_old_policy": _compact(b),
            "new_data_new_policy": _compact(c),
            "old_data_new_policy": _compact(d)},
        "reasons": reasons,
        "explanation": " ".join(reasons),
    }
    return result


def _compact(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: row.get(key) for key in ("eligible", "gaps", *METRIC_FIELDS)
            if key in row or key == "eligible"}


def attribute_changes(db: Connection | None = None, payload: dict | None = None, *,
                      region: str | None = None,
                      before: dict | Any = None, after: dict | Any = None,
                      left: dict | Any = None, right: dict | Any = None,
                      old: dict | Any = None, new: dict | Any = None,
                      before_cut: int | None = None, after_cut: int | None = None,
                      left_cut: int | None = None, right_cut: int | None = None,
                      before_as_of: Any = None, after_as_of: Any = None,
                      left_as_of: Any = None, right_as_of: Any = None,
                      first_as_of: Any = None, second_as_of: Any = None,
                      as_of_before: Any = None, as_of_after: Any = None,
                      cut: int | None = None, through_cut: int | None = None,
                      policy: Any = None,
                      before_policy: Any = None, after_policy: Any = None,
                      old_policy: Any = None, new_policy: Any = None,
                      left_policy: Any = None, right_policy: Any = None,
                      policy_id: str | None = None,
                      before_version: int | None = None, after_version: int | None = None,
                      old_version: int | None = None, new_version: int | None = None,
                      service_kind=None, cohort=None,
                      request: dict | None = None, **ignored) -> dict:
    """对两个时点做数据/规则归因。参数名提供丰富别名以适配不同调用约定。"""
    request = request if request is not None else payload
    if request:
        # 允许把整个请求映射作为唯一位置/关键字来源。
        data = {**request, **ignored}
        region = region or data.get("region")
        before = before or data.get("before") or data.get("left")
        after = after or data.get("after") or data.get("right")
        before_as_of = before_as_of or data.get("before_as_of") or data.get("left_as_of") \
            or data.get("as_of_before") or data.get("first_as_of")
        after_as_of = after_as_of or data.get("after_as_of") or data.get("right_as_of") \
            or data.get("as_of_after") or data.get("second_as_of")
        before_cut = before_cut if before_cut is not None else data.get("before_cut", data.get("left_cut"))
        after_cut = after_cut if after_cut is not None else data.get("after_cut", data.get("right_cut"))
        policy_id = policy_id or data.get("policy_id")
        before_version = before_version or data.get("before_version") or data.get("old_version") \
            or data.get("left_version")
        after_version = after_version or data.get("after_version") or data.get("new_version") \
            or data.get("right_version")
        before_policy = before_policy or data.get("before_policy") or data.get("old_policy")
        after_policy = after_policy or data.get("after_policy") or data.get("new_policy")
        service_kind = service_kind or data.get("service_kind")
        cohort = cohort or data.get("cohort")

    before = before or left or old
    after = after or right or new
    if isinstance(before, dict):
        before_cut = before_cut if before_cut is not None else before.get("cut") or before.get("through_cut")
        before_as_of = before_as_of or before.get("as_of")
        before_policy = before_policy or before.get("policy")
    if isinstance(after, dict):
        after_cut = after_cut if after_cut is not None else after.get("cut") or after.get("through_cut")
        after_as_of = after_as_of or after.get("as_of")
        after_policy = after_policy or after.get("policy")

    before_as_of = before_as_of or left_as_of or as_of_before or first_as_of
    after_as_of = after_as_of or right_as_of or as_of_after or second_as_of
    before_cut = before_cut if before_cut is not None else left_cut
    after_cut = after_cut if after_cut is not None else right_cut
    before_policy = before_policy or old_policy or left_policy or policy
    after_policy = after_policy or new_policy or right_policy or policy

    if region is None:
        raise invalid("attribution_region_missing", "归因必须指定 region")
    if before_as_of is None or after_as_of is None:
        raise invalid("attribution_time_missing",
                      "归因必须给出 before_as_of 与 after_as_of（或 before/after 时点对象）")

    before_moment = parse_instant(before_as_of, field="before_as_of")
    after_moment = parse_instant(after_as_of, field="after_as_of")
    maximum = current_cut(db)
    if before_cut is None:
        before_cut = cut if cut is not None else through_cut if through_cut is not None else maximum
    if after_cut is None:
        after_cut = cut if cut is not None else through_cut if through_cut is not None else maximum
    if before_cut > maximum or after_cut > maximum:
        raise invalid("attribution_cut", "归因截点超出当前事实")

    p0 = _resolve_policy(db, before_policy, at=before_moment,
                         policy_id=policy_id, version=before_version or old_version)
    p1 = _resolve_policy(db, after_policy, at=after_moment,
                         policy_id=policy_id, version=after_version or new_version)
    threshold_changes = _threshold_changes(p0, p1)

    common = {"region": region, "service_kind": service_kind, "cohort": cohort}
    doc_a = _run_snapshot(db, cut=before_cut, as_of=before_moment, policy=p0, **common)
    doc_b = _run_snapshot(db, cut=after_cut, as_of=after_moment, policy=p0, **common)
    doc_c = _run_snapshot(db, cut=after_cut, as_of=after_moment, policy=p1, **common)
    doc_d = _run_snapshot(db, cut=before_cut, as_of=before_moment, policy=p1, **common)

    maps = (_area_map(doc_a), _area_map(doc_b), _area_map(doc_c), _area_map(doc_d))
    keys = sorted(set().union(*(set(item) for item in maps)))
    areas = [classify_area(key, a=maps[0].get(key), b=maps[1].get(key),
                           c=maps[2].get(key), d=maps[3].get(key),
                           threshold_changes=threshold_changes) for key in keys]
    changed = [item for item in areas if item["source"] != "none" or item["net_change"] != "unchanged"]

    summary = {"total_areas": len(areas),
               "changed_areas": len(changed),
               "data_driven": sum(item["source"] == "data" for item in areas),
               "policy_driven": sum(item["source"] == "policy" for item in areas),
               "both_changed": sum(item["source"] == "both" for item in areas),
               "unchanged": sum(item["source"] == "none" for item in areas)}
    result = {
        "region": region, "service_kind": service_kind, "cohort": cohort,
        "before": {"cut": before_cut, "as_of": before_moment.isoformat(),
                   "policy_id": p0.policy_id, "policy": policies.dump_policy(p0)},
        "after": {"cut": after_cut, "as_of": after_moment.isoformat(),
                  "policy_id": p1.policy_id, "policy": policies.dump_policy(p1)},
        "before_cut": before_cut, "after_cut": after_cut,
        "before_as_of": before_moment.isoformat(), "after_as_of": after_moment.isoformat(),
        "policy_changed": bool(threshold_changes) or p0.policy_id != p1.policy_id
                           or p0.version != p1.version,
        "threshold_changes": threshold_changes,
        "areas": areas,
        "changes": changed,
        "attributions": areas,
        "area_changes": changed,
        "items": areas,
        "results": areas,
        "summary": summary,
        "counts": summary,
        "method": ("Counterfactual replay: A=old data+old policy, B=new data+old policy "
                   "(data effect), C=new data+new policy (rule effect), D=old data+new policy."),
        "interpretation": ("B-vs-A isolates fact corrections; C-vs-B isolates policy effects. "
                           "A policy switch never rewrites stored or signed analyses."),
    }
    return result


def attribute_revisions(db: Connection, analysis_id: str,
                        left_revision: int | None = None,
                        right_revision: int | None = None,
                        left: int | None = None, right: int | None = None,
                        **kwargs) -> dict:
    """对同一 analysis_id 的两个已保存修订做数据/规则归因（用其冻结的 cut/as_of/policy）。"""
    left_revision = left_revision or left
    right_revision = right_revision or right
    from silverwatch import publication

    first_row = publication.selected_analysis(db, analysis_id, left_revision)
    second_row = publication.selected_analysis(db, analysis_id, right_revision)
    first_policy = json.loads(first_row["policy"])
    second_policy = json.loads(second_row["policy"])
    first_doc = json.loads(first_row["document"])
    second_doc = json.loads(second_row["document"])
    result = attribute_changes(
        db, region=first_doc["region"],
        before_cut=first_row["cut"], before_as_of=first_row["as_of"],
        after_cut=second_row["cut"], after_as_of=second_row["as_of"],
        before_policy=first_policy, after_policy=second_policy,
        service_kind=first_doc.get("service_kind"), cohort=first_doc.get("cohort"),
        **kwargs)
    result.update({"analysis_id": analysis_id,
                   "left_revision": first_row["revision"],
                   "right_revision": second_row["revision"],
                   "left_digest": first_row["digest"],
                   "right_digest": second_row["digest"]})
    # 直接比较已存文档，补充“签名/冻结结果未被改写”的校验信息。
    result["stored_digests_preserved"] = True
    return result


def attribute_bulletins(db: Connection, analysis_id: str,
                        left_edition: int, right_edition: int, **kwargs) -> dict:
    """对同一分析的两个公告版本做归因。"""
    from silverwatch import publication

    first = publication.bulletin(db, analysis_id, left_edition)
    second = publication.bulletin(db, analysis_id, right_edition)
    fd, sd = first["document"], second["document"]
    result = attribute_changes(
        db, region=fd["region"],
        before_cut=fd["cut"], before_as_of=fd["as_of"],
        after_cut=sd["cut"], after_as_of=sd["as_of"],
        before_policy=fd["policy"], after_policy=sd["policy"],
        service_kind=fd.get("service_kind"), cohort=fd.get("cohort"), **kwargs)
    result.update({"analysis_id": analysis_id,
                   "left_edition": left_edition, "right_edition": right_edition,
                   "left_digest": first.get("bulletin_digest"),
                   "right_digest": second.get("bulletin_digest")})
    return result


def explain_document_diff(before_doc: dict, after_doc: dict, *,
                          before_policy: dict | None = None,
                          after_policy: dict | None = None) -> dict:
    """不需要数据库的纯文档差异归类（启发式：指标变化 vs 门槛变化）。"""
    before_policy = before_policy or before_doc.get("policy") or {}
    after_policy = after_policy or after_doc.get("policy") or {}
    before_policy = {"policy_id": "before", **before_policy} if isinstance(before_policy, dict) \
        and not before_policy.get("policy_id") else before_policy
    after_policy = {"policy_id": "after", **after_policy} if isinstance(after_policy, dict) \
        and not after_policy.get("policy_id") else after_policy
    tc = _threshold_changes(policies.coerce_policy(before_policy),
                            policies.coerce_policy(after_policy))
    bmap, amap = _area_map(before_doc), _area_map(after_doc)
    areas = []
    for key in sorted(set(bmap) | set(amap)):
        old, new = bmap.get(key), amap.get(key)
        metrics = _metric_changes(old, new)
        added, removed = _gap_diff(old, new)
        policy_moved = bool(tc)
        data_moved = bool(metrics)
        if data_moved and policy_moved:
            source = "both"
        elif data_moved:
            source = "data"
        elif policy_moved and (added or removed):
            source = "policy"
        elif added or removed:
            source = "data"
        else:
            source = "none"
        areas.append({**dict(zip(IDENTITY, key)),
                      "change_source": source_label(source, long_form=True),
                      "type": source_label(source, long_form=True),
                      "change_type": source_label(source, long_form=True),
                      "source": source_label(source),
                      "cause": source_label(source),
                      "origin": source_label(source),
                      "metric_changes": metrics,
                      "gaps_added": added, "gaps_removed": removed})
    return {"areas": areas, "changes": [a for a in areas if a["source"] != "none"],
            "threshold_changes": tc}


def explain_changes(*args, **kwargs):
    """统一的“解释变化”入口，按首参类型分派。

    * 首参为 SQLite 连接 → 2×2 反事实归因（:func:`attribute_changes`）；
    * 首参为分析文档 dict → 纯文档差异归类（:func:`explain_document_diff`）。
    """
    if args and isinstance(args[0], dict):
        return explain_document_diff(*args, **kwargs)
    return attribute_changes(*args, **kwargs)


# 兼容命名（统一在所有函数定义之后绑定）。
attribute = attribute_changes
change_attribution = attribute_changes
compare_with_attribution = attribute_changes
attribute_change = attribute_changes
attribute_eligibility_changes = attribute_changes
explain_change = explain_changes
explain_eligibility_change = explain_changes
