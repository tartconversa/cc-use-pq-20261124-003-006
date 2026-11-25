from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from sqlite3 import Connection

from silverwatch import analysis, policies
from silverwatch.contracts import AnalysisPolicy, ServiceKind
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut
from silverwatch.timeutil import parse_instant


def compare_regions(db: Connection, *, regions: list[str], as_of,
                    policy: AnalysisPolicy | dict | str | None = None,
                    cut: int | None = None,
                    through_cut: int | None = None,
                    service_kind: ServiceKind | None = None,
                    cohort: str | None = None,
                    policy_id: str | None = None,
                    policy_version: int | None = None,
                    version: int | None = None,
                    policy_mode: str | None = None,
                    mode: str | None = None,
                    request: dict | None = None,
                    **extra) -> dict:
    as_of = parse_instant(as_of, field="as_of", code="comparison_time")
    if not 2 <= len(regions) <= 10 or len(set(regions)) != len(regions):
        raise invalid("comparison_regions", "地区比较需提供 2 到 10 个互不重复的地区")
    if any(region not in {"urban", "suburban", "rural"} for region in regions):
        raise invalid("comparison_region", "比较请求含未知地区")
    selected_cut = cut if cut is not None else through_cut
    if selected_cut is None:
        selected_cut = current_cut(db)
    if selected_cut > current_cut(db):
        raise invalid("comparison_cut", "地区比较截点尚未存在")

    req = {**(request or {}), **extra, "as_of": as_of}
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
    selection = policies.select_policy(db, data=req, at=as_of, default_mode="effective")
    chosen_policy = selection["policy"]
    basis = selection["basis"]

    results = []
    for region in regions:
        document = analysis.run(db, cut=selected_cut, as_of=as_of, region=region,
                                service_kind=service_kind, cohort=cohort,
                                policy=chosen_policy, policy_basis=basis,
                                selection=selection)
        exposed = sum(row["exposed"] for row in document["areas"])
        nonresponse = sum(row["nonresponse"] for row in document["areas"])
        confirmed = sum(row["substantiated"] for row in document["areas"])
        pending = sum(row["pending"] for row in document["areas"])
        incomplete = sum(row["reconciliation_incomplete_batches"] for row in document["areas"])
        results.append({"region": region, "exposed": exposed, "nonresponse": nonresponse,
                        "confirmed": confirmed, "pending": pending,
                        "coverage_bp": round(exposed * 10_000 / (exposed + nonresponse))
                            if exposed + nonresponse else None,
                        "confirmed_rate_bp": round(float(Fraction(confirmed, exposed) * 10_000), 2)
                            if exposed else None,
                        "risk_area_count": len(document["risk_flags"]),
                        "unmatched_intake_batches": incomplete,
                        "supply": document["supply"],
                        "disclosure_state": "observed" if exposed else "no_exposure"})
    return {"cut": selected_cut, "as_of": as_of.isoformat(), "service_kind": service_kind,
            "cohort": cohort, "policy_id": selection["policy_id"],
            "policy_version": selection["version"], "version": selection["version"],
            "policy_mode": basis["mode"], "mode": basis["mode"],
            "policy_basis": basis,
            "applied_policy": policies.applied_policy_view(selection),
            "regions": results,
            "comparison_note": "Values are comparable only for the same time cut, policy, service and cohort; unknown exposure is not treated as zero risk.",
            "policy_note": basis["explanation"]}


def rank_by_confirmed_rate(result: dict) -> list[dict]:
    values = result["regions"]
    comparable = [row for row in values if row["confirmed_rate_bp"] is not None]
    comparable.sort(key=lambda row: (-row["confirmed_rate_bp"], row["region"]))
    return [{"rank": index + 1, **row} for index, row in enumerate(comparable)]
