from decimal import Decimal
from ..validation import (
    name,
    object_value,
    integer,
    decimal,
    decimal_text,
    canonical,
    digest,
    parse_json,
    reject,
)
from .statistics import (
    mean,
    stratified_effect,
    interval,
    permutation_test,
    seed_value,
    ols_residuals,
    holm_adjust,
)


class AnalysisRuns:
    """Executes a locked plan against a fixed outcome version.

    Runs are stored immutably and are idempotent: the same run id with the
    same request returns the stored result, and a different request with the
    same id is rejected. Late-arriving data produces a new outcome version
    and therefore a new run; frozen runs never change in place.
    """

    def __init__(self, database, registry, ingestion, plans):
        self.db = database
        self.registry = registry
        self.ingestion = ingestion
        self.plans = plans
        with database.transaction() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS analysis_runs("
                "id TEXT PRIMARY KEY,request TEXT NOT NULL,result TEXT NOT NULL)"
            )

    def get(self, run_id):
        row = self.db.one("SELECT result FROM analysis_runs WHERE id=?", (run_id,))
        if row is None:
            reject("missing", "analysis run not found")
        return parse_json(row["result"])

    def run(self, run_id, plan_id, plan_version, outcomes):
        name(run_id)
        plan = self.plans.get(plan_id, integer(plan_version, 1, 1000000))
        object_value(outcomes, {"dataset", "version"}, {"dataset", "version"})
        outcome_dataset = name(outcomes["dataset"])
        outcome_version = integer(outcomes["version"], 1)
        request = {
            "plan": {"id": plan["id"], "version": plan["version"]},
            "outcomes": {"dataset": outcome_dataset, "version": outcome_version},
        }
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT request,result FROM analysis_runs WHERE id=?", (run_id,)
            )
            if old:
                if old["request"] != canonical(request):
                    reject("conflict", "analysis run identity differs")
                return parse_json(old["result"])
            assignment_record = self.registry.version(
                plan["assignment"]["dataset"], plan["assignment"]["version"]
            )
            if assignment_record["state"] != "active":
                reject("state", "assignment version is withdrawn")
            if assignment_record["digest"] != plan["assignment"]["digest"]:
                reject("integrity", "assignment version differs from locked plan")
            outcome_record = self.registry.version(outcome_dataset, outcome_version)
            if outcome_record["state"] != "active":
                reject("state", "outcome version is withdrawn")
            assignment_schema = self.registry.schema(
                self.registry.dataset(plan["assignment"]["dataset"])["schema_id"],
                assignment_record["schema_version"],
            )
            outcome_schema = self.registry.schema(
                self.registry.dataset(outcome_dataset)["schema_id"],
                outcome_record["schema_version"],
            )
            result = execute(
                run_id,
                plan,
                assignment_record,
                outcome_record,
                self.ingestion.rows(
                    plan["assignment"]["dataset"], plan["assignment"]["version"]
                ),
                self.ingestion.rows(outcome_dataset, outcome_version),
                assignment_schema,
                outcome_schema,
            )
            con.execute(
                "INSERT INTO analysis_runs VALUES(?,?,?)",
                (run_id, canonical(request), canonical(result)),
            )
            self.db.audit(
                "analysis_run",
                run_id,
                {
                    "plan": plan["id"],
                    "plan_version": plan["version"],
                    "outcomes": outcome_dataset,
                    "outcomes_version": outcome_version,
                    "digest": digest(result),
                },
            )
        return result


def execute(
    run_id,
    plan,
    assignment_record,
    outcome_record,
    assignment_rows,
    outcome_rows,
    assignment_schema,
    outcome_schema,
):
    _check_outcome_schema(plan, assignment_schema, outcome_schema)
    unit = plan["unit"]
    arm_field = plan["arm"]["field"]
    treatment_label = plan["arm"]["treatment"]
    cutoff_field = plan["cutoff"]["field"]
    covariate_fields = (plan["adjustment"] or {}).get("covariates", [])
    explanations = []

    enrolled = {}
    for row in assignment_rows:
        enrolled[row[unit]] = {
            "arm": row[arm_field],
            "stratum": [row[field] for field in plan["strata"]],
            "covariates": {field: row.get(field) for field in covariate_fields},
        }
    enrollment_counts = {}
    for info in enrolled.values():
        key = canonical(info["stratum"])
        enrollment_counts[key] = enrollment_counts.get(key, 0) + 1
    total_enrolled = len(assignment_rows)

    chosen, excluded_rows, repeated = _disposition(plan, enrolled, outcome_rows)

    primary = plan["endpoints"]["primary"]
    bounds = plan["preprocessing"]["bounds"]
    primary_values, primary_missing, primary_bounds = _cohort(chosen, primary, bounds)
    analyzed_units = sorted(primary_values)
    unmeasured = sorted((set(enrolled) - set(chosen)) | set(primary_missing))
    actual_field = plan["actual_arm_field"]
    crossovers = []
    for unit_value in analyzed_units:
        actual = chosen[unit_value].get(actual_field)
        if actual is not None and actual != enrolled[unit_value]["arm"]:
            crossovers.append(
                {
                    "unit": unit_value,
                    "assigned": enrolled[unit_value]["arm"],
                    "actual": actual,
                }
            )

    adjustment_info = None
    unadjusted = None
    analysis_values = primary_values
    if plan["adjustment"] is not None:
        fitted = [
            unit_value
            for unit_value in analyzed_units
            if all(
                enrolled[unit_value]["covariates"][field] is not None
                for field in covariate_fields
            )
        ]
        incomplete = len(analyzed_units) - len(fitted)
        residuals = ols_residuals(
            [
                [decimal(enrolled[unit_value]["covariates"][field]) for field in covariate_fields]
                for unit_value in fitted
            ],
            [primary_values[unit_value] for unit_value in fitted],
        )
        analysis_values = dict(zip(fitted, residuals))
        adjustment_info = {
            "covariates": covariate_fields,
            "fitted_count": len(fitted),
            "fitted_units": fitted,
            "fitted_digest": digest(fitted),
            "excluded_incomplete_covariates": incomplete,
        }
        explanations.append(
            {
                "code": "adjustment_fit",
                "message": "covariate adjustment uses pre-enrollment data only; "
                "fitted sample is recorded",
                "endpoint": primary["id"],
                "fitted_count": len(fitted),
            }
        )
        if incomplete:
            explanations.append(
                {
                    "code": "adjustment_complete_cases",
                    "message": "units with incomplete pre-enrollment covariates are "
                    "excluded from the adjusted analysis",
                    "endpoint": primary["id"],
                    "count": incomplete,
                }
            )
        unadjusted = _point_estimate(
            primary_values, enrolled, enrollment_counts, total_enrolled, plan, primary["kind"]
        )

    primary_result = _endpoint_analysis(
        primary, analysis_values, enrolled, enrollment_counts, total_enrolled, plan, explanations
    )
    primary_result["adjustment"] = adjustment_info
    if unadjusted is not None:
        primary_result["unadjusted"] = unadjusted

    secondary_results = []
    family = []
    for endpoint in plan["endpoints"]["secondary"]:
        values, missing, out_of_bounds = _cohort(chosen, endpoint, bounds)
        result = _endpoint_analysis(
            endpoint, values, enrolled, enrollment_counts, total_enrolled, plan, explanations
        )
        result["missing_value"] = len(missing)
        result["excluded_bounds"] = len(out_of_bounds)
        secondary_results.append(result)
        if result["estimable"]:
            family.append(
                (len(secondary_results) - 1, Decimal(result["test"]["p_value"]))
            )
    if family:
        adjusted = holm_adjust([p_value for _, p_value in family])
        ranks = _ranks([p_value for _, p_value in family])
        for position, (index, _) in enumerate(family):
            secondary_results[index]["holm"] = {
                "family_size": len(family),
                "rank": ranks[position],
                "p_adjusted": decimal_text(adjusted[position]),
            }
    for result in secondary_results:
        result.setdefault("holm", None)

    after_cutoff = sum(1 for item in excluded_rows if item["reason"] == "after_cutoff")
    if after_cutoff:
        explanations.append(
            {
                "code": "after_cutoff",
                "message": "outcome rows observed after the planned cutoff are excluded",
                "count": after_cutoff,
            }
        )
    unassigned = sum(1 for item in excluded_rows if item["reason"] == "unassigned_unit")
    if unassigned:
        explanations.append(
            {
                "code": "unassigned_unit",
                "message": "outcome rows reference units absent from the locked assignment",
                "count": unassigned,
            }
        )
    if unmeasured:
        explanations.append(
            {
                "code": "unmeasured",
                "message": "assigned units without a usable pre-cutoff primary measurement",
                "count": len(unmeasured),
            }
        )
    if primary_bounds:
        explanations.append(
            {
                "code": "bounds",
                "message": "primary values outside planned preprocessing bounds are excluded",
                "count": len(primary_bounds),
            }
        )
    if crossovers:
        explanations.append(
            {
                "code": "crossover",
                "message": "units received a different arm than assigned and are "
                "analyzed as assigned",
                "count": len(crossovers),
            }
        )
    if repeated:
        explanations.append(
            {
                "code": "repeated",
                "message": "repeated measurements resolved by the planned rule",
                "count": len(repeated),
            }
        )
    enrolled_treated = sum(
        1 for info in enrolled.values() if info["arm"] == treatment_label
    )
    analyzed_treated = sum(
        1 for unit_value in analyzed_units if enrolled[unit_value]["arm"] == treatment_label
    )
    explanations.append(
        {
            "code": "allocation",
            "message": "arm allocation across enrollment and the analyzed cohort",
            "enrolled_treatment": enrolled_treated,
            "enrolled_control": total_enrolled - enrolled_treated,
            "analyzed_treatment": analyzed_treated,
            "analyzed_control": len(analyzed_units) - analyzed_treated,
        }
    )

    return {
        "id": run_id,
        "plan": {"id": plan["id"], "version": plan["version"], "digest": digest(plan)},
        "unit": plan["unit"],
        "inputs": {
            "assignment": {
                "dataset": plan["assignment"]["dataset"],
                "version": plan["assignment"]["version"],
                "digest": assignment_record["digest"],
                "artifact": assignment_record["artifact"],
            },
            "outcomes": {
                "dataset": outcome_record["dataset"],
                "version": outcome_record["version"],
                "digest": outcome_record["digest"],
                "artifact": outcome_record["artifact"],
            },
        },
        "seed": plan["seed"],
        "permutations": plan["permutations"],
        "confidence": plan["confidence"],
        "disposition": {
            "assigned": total_enrolled,
            "analyzed_primary": len(analyzed_units),
            "unmeasured": unmeasured,
            "excluded_bounds": sorted(primary_bounds),
            "crossovers": crossovers,
            "repeated": repeated,
            "excluded_rows": excluded_rows,
        },
        "primary": primary_result,
        "secondary": secondary_results,
        "explanations": explanations,
    }


def _check_outcome_schema(plan, assignment_schema, outcome_schema):
    unit = plan["unit"]
    definition = outcome_schema.by_name.get(unit)
    if definition is None:
        reject("analysis", "outcome data lacks the experimental unit field")
    if definition.type != assignment_schema.by_name[unit].type:
        reject("analysis", "outcome unit field type differs from assignment")
    cutoff = outcome_schema.by_name.get(plan["cutoff"]["field"])
    if cutoff is None or cutoff.type != "timestamp":
        reject("analysis", "outcome cutoff field must be a timestamp")
    endpoints = [plan["endpoints"]["primary"]] + plan["endpoints"]["secondary"]
    for endpoint in endpoints:
        field = outcome_schema.by_name.get(endpoint["field"])
        if field is None:
            reject(
                "analysis",
                "outcome data lacks planned endpoint field",
                endpoint=endpoint["id"],
            )
        if endpoint["kind"] == "continuous" and field.type not in {"integer", "decimal"}:
            reject(
                "analysis",
                "continuous endpoint requires numeric outcome field",
                endpoint=endpoint["id"],
            )
        if endpoint["kind"] == "binary" and field.type != "boolean":
            reject(
                "analysis",
                "binary endpoint requires boolean outcome field",
                endpoint=endpoint["id"],
            )
    actual = outcome_schema.by_name.get(plan["actual_arm_field"])
    if actual is None or actual.type not in {"text", "enum"}:
        reject("analysis", "outcome data lacks usable actual arm field")


def _disposition(plan, enrolled, outcome_rows):
    unit = plan["unit"]
    cutoff_field = plan["cutoff"]["field"]
    cutoff_at = plan["cutoff"]["at"]
    latest = plan["preprocessing"]["repeated"] == "latest_before_cutoff"
    chosen = {}
    excluded_rows = []
    repeated = []
    for row in outcome_rows:
        unit_value = row.get(unit)
        if unit_value is None:
            excluded_rows.append({"unit": None, "reason": "missing_unit"})
            continue
        if unit_value not in enrolled:
            excluded_rows.append({"unit": unit_value, "reason": "unassigned_unit"})
            continue
        observed_at = row.get(cutoff_field)
        if observed_at is None:
            excluded_rows.append(
                {"unit": unit_value, "reason": "unknown_observation_time"}
            )
            continue
        if observed_at > cutoff_at:
            excluded_rows.append(
                {
                    "unit": unit_value,
                    "reason": "after_cutoff",
                    "observed_at": observed_at,
                }
            )
            continue
        previous = chosen.get(unit_value)
        if previous is None:
            chosen[unit_value] = row
            continue
        keep_new = (
            observed_at > previous[cutoff_field]
            if latest
            else observed_at < previous[cutoff_field]
        )
        kept, dropped = (row, previous) if keep_new else (previous, row)
        repeated.append(
            {
                "unit": unit_value,
                "kept_at": kept[cutoff_field],
                "dropped_at": dropped[cutoff_field],
            }
        )
        if keep_new:
            chosen[unit_value] = row
    return chosen, excluded_rows, repeated


def _cohort(chosen, endpoint, bounds):
    values = {}
    missing = []
    out_of_bounds = []
    for unit_value, row in chosen.items():
        raw = row.get(endpoint["field"])
        if raw is None:
            missing.append(unit_value)
            continue
        if endpoint["kind"] == "binary":
            values[unit_value] = Decimal(1) if raw else Decimal(0)
            continue
        number = decimal(raw)
        if bounds is not None and (
            number < decimal(bounds["minimum"]) or number > decimal(bounds["maximum"])
        ):
            out_of_bounds.append(unit_value)
            continue
        values[unit_value] = number
    return values, missing, out_of_bounds


def _group(values, enrolled, plan):
    treatment_label = plan["arm"]["treatment"]
    grouped = {}
    for unit_value, value in values.items():
        info = enrolled[unit_value]
        key = canonical(info["stratum"])
        arms = grouped.setdefault(key, {"treated": [], "control": []})
        arms["treated" if info["arm"] == treatment_label else "control"].append(value)
    return grouped


def _point_estimate(values, enrolled, enrollment_counts, total_enrolled, plan, kind):
    grouped = _group(values, enrolled, plan)
    contributing = []
    for key in sorted(enrollment_counts):
        arms = grouped.get(key, {"treated": [], "control": []})
        if arms["treated"] and arms["control"]:
            contributing.append(
                {
                    "weight": Decimal(enrollment_counts[key]) / Decimal(total_enrolled),
                    "treated": arms["treated"],
                    "control": arms["control"],
                }
            )
    if not contributing:
        return None
    estimate, variance, _ = stratified_effect(contributing, kind)
    return {
        "estimate": decimal_text(estimate),
        "standard_error": decimal_text(variance.sqrt()),
    }


def _endpoint_analysis(
    endpoint, values, enrolled, enrollment_counts, total_enrolled, plan, explanations
):
    grouped = _group(values, enrolled, plan)
    contributing = []
    details = []
    dropped_weight = Decimal(0)
    for key in sorted(enrollment_counts):
        enrolled_count = enrollment_counts[key]
        weight = Decimal(enrolled_count) / Decimal(total_enrolled)
        arms = grouped.get(key, {"treated": [], "control": []})
        detail = {
            "stratum": dict(zip(plan["strata"], parse_json(key))),
            "enrolled": enrolled_count,
            "treated": len(arms["treated"]),
            "control": len(arms["control"]),
        }
        if not arms["treated"] or not arms["control"]:
            dropped_weight += weight
            detail["contributing"] = False
            explanations.append(
                {
                    "code": "empty_stratum",
                    "message": "stratum has no analyzed units in one arm and is "
                    "excluded from estimation",
                    "endpoint": endpoint["id"],
                    "stratum": detail["stratum"],
                    "treated": len(arms["treated"]),
                    "control": len(arms["control"]),
                }
            )
        else:
            detail["contributing"] = True
            contributing.append(
                {
                    "weight": weight,
                    "treated": arms["treated"],
                    "control": arms["control"],
                    "detail": detail,
                }
            )
        details.append(detail)
    if not contributing:
        explanations.append(
            {
                "code": "no_contributing_stratum",
                "message": "no stratum has analyzed units in both arms; endpoint "
                "is not estimable",
                "endpoint": endpoint["id"],
            }
        )
        return {
            "id": endpoint["id"],
            "kind": endpoint["kind"],
            "field": endpoint["field"],
            "analyzed": len(values),
            "estimable": False,
            "estimate": None,
            "standard_error": None,
            "interval": None,
            "test": None,
            "strata": details,
        }
    estimate, variance, total_weight = stratified_effect(contributing, endpoint["kind"])
    for item in contributing:
        detail = item["detail"]
        detail["weight"] = decimal_text(item["weight"] / total_weight)
        detail["effect"] = decimal_text(
            mean(item["treated"]) - mean(item["control"])
        )
        if len(item["treated"]) == 1 or len(item["control"]) == 1:
            explanations.append(
                {
                    "code": "singleton_arm",
                    "message": "arm with a single analyzed unit contributes zero "
                    "within-stratum variance",
                    "endpoint": endpoint["id"],
                    "stratum": detail["stratum"],
                }
            )
        if len(set(item["treated"])) == 1 and len(set(item["control"])) == 1:
            explanations.append(
                {
                    "code": "constant_stratum",
                    "message": "outcome is constant within both arms of the stratum; "
                    "variance contribution is zero",
                    "endpoint": endpoint["id"],
                    "stratum": detail["stratum"],
                }
            )
    if dropped_weight > 0:
        explanations.append(
            {
                "code": "weights_renormalized",
                "message": "enrollment weights renormalized over contributing strata",
                "endpoint": endpoint["id"],
                "dropped_weight": decimal_text(dropped_weight),
            }
        )
    standard_error, low, high = interval(estimate, variance, plan["confidence"])
    if standard_error == 0:
        explanations.append(
            {
                "code": "degenerate_interval",
                "message": "standard error is zero; the interval collapses to the "
                "point estimate",
                "endpoint": endpoint["id"],
            }
        )
    float_strata = [
        (
            [float(value) for value in item["treated"]]
            + [float(value) for value in item["control"]],
            len(item["treated"]),
            float(item["weight"] / total_weight),
        )
        for item in contributing
    ]
    _, exceedances, p_value = permutation_test(
        float_strata,
        plan["permutations"],
        seed_value("trialvault", plan["seed"], endpoint["id"]),
    )
    return {
        "id": endpoint["id"],
        "kind": endpoint["kind"],
        "field": endpoint["field"],
        "analyzed": len(values),
        "estimable": True,
        "estimate": decimal_text(estimate),
        "standard_error": decimal_text(standard_error),
        "interval": {
            "low": decimal_text(low),
            "high": decimal_text(high),
            "level": plan["confidence"],
            "method": "normal_approximation",
            "assumption": "enrollment-weighted stratified estimate; standard error "
            "from within-stratum sample variances; bounds assume approximate "
            "normality of the weighted estimator",
        },
        "test": {
            "method": "stratified_randomization_permutation",
            "p_value": decimal_text(p_value),
            "permutations": plan["permutations"],
            "exceedances": exceedances,
            "assumption": "arm labels permuted at unit level within locked strata; "
            "enrollment weights held fixed; two-sided exceedance with Monte "
            "Carlo correction",
        },
        "strata": details,
    }


def _ranks(p_values):
    order = sorted(range(len(p_values)), key=lambda i: (p_values[i], i))
    ranks = [0] * len(p_values)
    for rank, index in enumerate(order, 1):
        ranks[index] = rank
    return ranks
