from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection

from silverwatch import analysis, policies
from silverwatch.contracts import AnalysisPolicy, ServiceKind
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut
from silverwatch.timeutil import parse_instant


def _rate(row: dict) -> float | None:
    value = row["substantiated_rate"]
    return float(value.removesuffix("bp")) if value is not None else None


def _index(document: dict) -> dict:
    return {(row["district"], row["service_kind"], row["cohort"]): row
            for row in document["areas"]}


def _strategy(db: Connection, *, policy, request: dict) -> str:
    mode = policies.normalize_mode(request.get("policy_mode") or request.get("mode")
                                   or request.get("selection_mode"))
    for key in policies._FIXED_BOOL_KEYS:
        if request.get(key):
            return "fixed"
    for key in getattr(policies, "_EFFECTIVE_BOOL_KEYS", ()):
        if request.get(key):
            return "effective"
    if mode == "fixed":
        return "fixed"
    if mode == "effective":
        return "effective"
    if request.get("policy_version") or request.get("version"):
        return "fixed"
    pid = request.get("policy_id") or request.get("policy_ref")
    if pid and db is not None and db.execute(
            "SELECT 1 FROM policy_version WHERE policy_id=? LIMIT 1", (str(pid),)).fetchone():
        return "effective"
    if isinstance(policy, str):
        return "effective"
    if isinstance(request.get("policy_schedule") or request.get("schedule"), list):
        return "effective"
    # 内联政策对象：所有期沿用同一口径（历史行为）。
    return "inline"


def _from_schedule(schedule: list, moment: datetime) -> dict | None:
    """从调用方直接给出的政策时间表中选择 moment 时点生效的版本。"""
    entries = []
    for raw in schedule:
        if not isinstance(raw, dict):
            continue
        at = raw.get("effective_from") or raw.get("from") or raw.get("effective_date")
        if at is None:
            continue
        start = parse_instant(at, field="effective_from")
        body = raw.get("policy") if isinstance(raw.get("policy"), dict) else raw
        entries.append((start, raw, body))
    entries.sort(key=lambda item: item[0])
    applicable = [item for item in entries if item[0] <= moment]
    if not applicable:
        return None
    _, raw, body = applicable[-1]
    policy = policies.coerce_policy(body)
    record = {"policy_id": policy.policy_id,
              "version": raw.get("version") or raw.get("policy_version") or policy.version,
              "effective_from": applicable[-1][0].isoformat(),
              "effective_to": raw.get("effective_to"), "policy": policies.dump_policy(policy)}
    basis = policies.build_basis(mode="effective", record=record, at=moment)
    return {"mode": "effective", "policy": policy, "record": record,
            "policy_id": policy.policy_id, "version": record["version"], "basis": basis}


def build(db: Connection, *, region: str, periods: list[dict],
          policy: AnalysisPolicy | dict | str | None = None,
          service_kind: ServiceKind | None = None,
          cohort: str | None = None,
          policy_id: str | None = None,
          policy_version: int | None = None,
          version: int | None = None,
          policy_mode: str | None = None,
          mode: str | None = None,
          request: dict | None = None,
          payload: dict | None = None,
          **extra) -> dict:
    if not 2 <= len(periods) <= 52:
        raise invalid("trend_periods", "趋势至少包含 2 期且最多包含 52 期")
    req = {**(request or {}), **(payload or {}), **extra}
    if policy is not None:
        req["policy"] = policy
    if policy_id is not None:
        req["policy_id"] = policy_id
    if policy_version is not None:
        req["policy_version"] = policy_version
    if version is not None:
        req["version"] = version
    if policy_mode is not None:
        req["policy_mode"] = policy_mode
    if mode is not None:
        req["mode"] = mode

    current = current_cut(db)
    parsed = []
    previous_cut = -1
    previous_time = None
    for index, raw_value in enumerate(periods):
        if not isinstance(raw_value, dict):
            raise invalid("trend_period_shape", "每一期须含 label、cut 与 as_of", {"position": index})
        # 兼容常见别名字段。
        value = dict(raw_value)
        value.setdefault("as_of", value.get("at") or value.get("date") or value.get("time"))
        value.setdefault("label", value.get("period") or value.get("name"))
        if not {"label", "as_of"} <= set(value):
            raise invalid("trend_period_shape", "每一期须含 label 与 as_of（cut 可省略）",
                          {"position": index})
        as_of = parse_instant(value["as_of"], field="as_of", code="trend_period_value")
        cut = value.get("cut", current)
        if not isinstance(cut, int) or isinstance(cut, bool):
            raise invalid("trend_period_value", "趋势截点必须是整数序号", {"position": index})
        if cut < previous_cut or cut > current or previous_time and as_of <= previous_time:
            raise invalid("trend_order", "趋势时间必须递增，序号截点不得倒退或超前",
                          {"position": index, "previous_cut": previous_cut, "current_cut": cut})
        if not isinstance(value["label"], str) or not value["label"].strip():
            raise invalid("trend_label", "趋势期必须有非空名称", {"position": index})
        parsed.append({"label": value["label"].strip(), "cut": cut, "as_of": as_of})
        previous_cut, previous_time = cut, as_of

    strategy = _strategy(db, policy=req.get("policy"), request=req)

    fixed_selection = None
    if strategy == "fixed":
        fixed_selection = policies.select_policy(db, data=req, at=parsed[-1]["as_of"],
                                                default_mode="fixed")
    elif strategy == "inline":
        inline_policy = policies.coerce_policy(req.get("policy"), policy_id=policy_id)
        fixed_selection = {
            "mode": "inline", "policy": inline_policy,
            "policy_id": inline_policy.policy_id,
            "version": inline_policy.version or inline_policy.policy_version,
            "basis": policies.build_basis(
                mode="inline",
                record={"policy_id": inline_policy.policy_id,
                        "version": inline_policy.version,
                        "policy": policies.dump_policy(inline_policy)})}

    snapshots = []
    schedule = []
    for point in parsed:
        if strategy in {"fixed", "inline"}:
            selected_policy = fixed_selection["policy"]
            basis = dict(fixed_selection["basis"])
            basis["as_of"] = point["as_of"].isoformat()
            basis["evaluated_at"] = point["as_of"].isoformat()
            if strategy == "fixed":
                basis["mode"] = policies.mode_label("fixed")
            selection = {**fixed_selection, "basis": basis}
        else:
            explicit_schedule = req.get("policy_schedule")
            if not isinstance(explicit_schedule, list):
                explicit_schedule = req.get("schedule") if isinstance(req.get("schedule"), list) else None
            selection = None
            if explicit_schedule:
                selection = _from_schedule(explicit_schedule, point["as_of"])
            if selection is None:
                selection = policies.select_policy(db, data=req, at=point["as_of"],
                                                   default_mode="effective")
            selected_policy = selection["policy"]
            basis = selection["basis"]
        document = analysis.run(db, cut=point["cut"], as_of=point["as_of"], region=region,
                                service_kind=service_kind, cohort=cohort,
                                policy=selected_policy, policy_basis=basis,
                                selection=selection)
        policy_info = {"policy_id": selection["policy_id"],
                       "policy_version": selection["version"],
                       "version": selection["version"],
                       "policy_mode": basis["mode"], "mode": basis["mode"],
                       "policy_ref": basis.get("policy_ref")}
        snapshots.append({**point, "document": document, "areas": _index(document),
                          "policy_info": policy_info})
        schedule.append({"label": point["label"], "cut": point["cut"],
                         "as_of": point["as_of"].isoformat(), **policy_info,
                         "effective_from": basis.get("effective_from"),
                         "effective_to": basis.get("resolved_effective_to"),
                         "policy_basis": basis})

    keys = sorted(set().union(*(item["areas"].keys() for item in snapshots))) if snapshots else []
    series = []
    for key in keys:
        points = []
        previous = None
        for snapshot in snapshots:
            row = snapshot["areas"].get(key)
            info = snapshot["policy_info"]
            if row is None:
                points.append({"period": snapshot["label"], "present": False,
                               "exposed": 0, "reports": 0, "substantiated": 0,
                               "pending": 0, "substantiated_rate_bp": None,
                               "coverage_complete": None, "rate_delta_bp": None,
                               **info})
                previous = None
                continue
            rate = _rate(row)
            prior_rate = _rate(previous) if previous else None
            delta = round(rate - prior_rate, 2) if rate is not None and prior_rate is not None else None
            points.append({"period": snapshot["label"], "present": True,
                           "exposed": row["exposed"], "nonresponse": row["nonresponse"],
                           "reports": row["reports"], "substantiated": row["substantiated"],
                           "pending": row["pending"], "inconclusive": row["inconclusive"],
                           "substantiated_rate_bp": rate,
                           "potential_rate_bounds": row["potential_rate_bounds"],
                           "coverage_complete": row["coverage_unknown_batches"] == 0,
                           "eligible": row["eligible"], "gaps": row["gaps"],
                           "rate_delta_bp": delta, **info})
            previous = row
        series.append({"district": key[0], "service_kind": key[1],
                       "cohort": key[2], "points": points})

    switches = []
    for left, right in zip(schedule, schedule[1:]):
        if left.get("policy_version") != right.get("policy_version") \
                or left.get("policy_id") != right.get("policy_id"):
            switches.append({
                "from_period": left["label"], "to_period": right["label"],
                "between": [left["label"], right["label"]],
                "as_of": right["as_of"],
                "policy_id": right["policy_id"],
                "from_version": left.get("policy_version"),
                "to_version": right.get("policy_version"),
                "old_version": left.get("policy_version"),
                "new_version": right.get("policy_version"),
                "effective_from": right.get("effective_from"),
                "explanation": right["policy_basis"].get("explanation")})

    distinct_versions = {}
    for entry in schedule:
        ref = entry.get("policy_ref") or f"{entry['policy_id']}@v{entry.get('policy_version')}"
        distinct_versions[ref] = {"policy_id": entry["policy_id"],
                                 "version": entry.get("policy_version"),
                                 "policy_version": entry.get("policy_version"),
                                 "policy_ref": ref,
                                 "effective_from": entry.get("effective_from"),
                                 "effective_to": entry.get("effective_to")}

    if strategy == "fixed":
        policy_id_value = fixed_selection["policy_id"]
        basis_summary = fixed_selection["basis"]
        policy_mode = "fixed" if req.get("policy_id") or req.get("policy_version") else "inline"
        uniform = True
        policy_note = ("All periods were recalculated under one fixed policy so the trend is "
                       "like-for-like; eligibility changes therefore reflect data, not rule switches.")
    elif strategy == "effective":
        policy_id_value = req.get("policy_id") or req.get("policy_ref")
        basis_summary = {"mode": "effective", "policy_id": policy_id_value}
        policy_mode = "effective"
        uniform = False
        policy_note = ("Each period is scored under the policy version effective at its own time; "
                       "a policy switch can change eligibility across the boundary. Use the fixed "
                       "policy replay for like-for-like historical comparison.")
    else:
        policy_id_value = fixed_selection["policy_id"]
        basis_summary = fixed_selection["basis"]
        policy_mode = "inline"
        uniform = True
        policy_note = "The same inline policy was applied to every period."

    return {"region": region, "service_kind": service_kind, "cohort": cohort,
            "policy_id": policy_id_value,
            "policy_mode": policies.mode_label(policy_mode),
            "mode": policies.mode_label(policy_mode),
            "uniform_policy": uniform,
            "same_policy_replay": uniform and strategy == "fixed",
            "policy_basis": basis_summary,
            "policy_schedule": schedule,
            "schedule": schedule,
            "period_policy": schedule,
            "policy_versions": list(distinct_versions.values()),
            "policies": schedule,
            "policy_switches": switches,
            "switches": switches,
            "periods": [{"label": point["label"], "cut": point["cut"],
                         "as_of": point["as_of"].isoformat(),
                         "policy_id": schedule[index]["policy_id"],
                         "policy_version": schedule[index]["policy_version"]}
                        for index, point in enumerate(parsed)],
            "series": series,
            "interpretation": "A change in the confirmed rate is shown alongside its denominator, pending cases and coverage; the change alone does not establish a cause.",
            "policy_note": policy_note,
            "comparability_note": policy_note}
