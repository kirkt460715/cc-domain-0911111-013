from ..validation import (
    name,
    object_value,
    list_value,
    integer,
    decimal,
    decimal_text,
    moment,
    canonical,
    digest,
    parse_json,
    reject,
)
from .statistics import CONFIDENCE_Z

REPEATED_RULES = {"latest_before_cutoff", "earliest_before_cutoff"}
ENDPOINT_KINDS = {"continuous", "binary"}


class AnalysisPlans:
    """Locked analysis plans pinned to a fixed randomization version.

    A plan records the experimental unit, strata, primary and secondary
    endpoints, observation cutoff and allowed preprocessing before any
    outcome is seen. Plans are immutable: changes require a revision that
    creates a new version linked to its parent.
    """

    def __init__(self, database, registry, ingestion):
        self.db = database
        self.registry = registry
        self.ingestion = ingestion
        with database.transaction() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS analysis_plans("
                "id TEXT NOT NULL,version INTEGER NOT NULL,"
                "payload TEXT NOT NULL,digest TEXT NOT NULL,"
                "PRIMARY KEY(id,version))"
            )

    def define(
        self,
        plan_id,
        title,
        assignment,
        unit,
        arm,
        primary,
        cutoff,
        actual_arm_field,
        seed,
        strata=None,
        secondary=None,
        preprocessing=None,
        adjustment=None,
        permutations=2000,
        confidence="0.95",
    ):
        payload = self._build(
            plan_id,
            1,
            None,
            None,
            title,
            assignment,
            unit,
            arm,
            strata,
            primary,
            secondary,
            cutoff,
            actual_arm_field,
            preprocessing,
            adjustment,
            permutations,
            seed,
            confidence,
        )
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT payload,digest FROM analysis_plans WHERE id=? AND version=1",
                (plan_id,),
            )
            if old:
                if old["digest"] != digest(payload):
                    reject(
                        "conflict",
                        "analysis plan is immutable; request a revision instead",
                    )
                return parse_json(old["payload"])
            con.execute(
                "INSERT INTO analysis_plans VALUES(?,?,?,?)",
                (plan_id, 1, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "analysis_plan_defined", plan_id, {"version": 1, "digest": digest(payload)}
            )
        return payload

    def revise(
        self,
        plan_id,
        reason,
        title,
        assignment,
        unit,
        arm,
        primary,
        cutoff,
        actual_arm_field,
        seed,
        strata=None,
        secondary=None,
        preprocessing=None,
        adjustment=None,
        permutations=2000,
        confidence="0.95",
    ):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            reject("input", "plan revision requires bounded reason")
        with self.db.transaction() as con:
            latest = self.db.one(
                "SELECT version,payload,digest FROM analysis_plans WHERE id=? "
                "ORDER BY version DESC LIMIT 1",
                (plan_id,),
            )
            if latest is None:
                reject("missing", "analysis plan not found")
            parent = {
                "id": plan_id,
                "version": latest["version"],
                "digest": latest["digest"],
            }
            payload = self._build(
                plan_id,
                latest["version"] + 1,
                parent,
                reason.strip(),
                title,
                assignment,
                unit,
                arm,
                strata,
                primary,
                secondary,
                cutoff,
                actual_arm_field,
                preprocessing,
                adjustment,
                permutations,
                seed,
                confidence,
            )
            if _content(payload) == _content(parse_json(latest["payload"])):
                reject("conflict", "plan revision must change analysis content")
            con.execute(
                "INSERT INTO analysis_plans VALUES(?,?,?,?)",
                (plan_id, payload["version"], canonical(payload), digest(payload)),
            )
            self.db.audit(
                "analysis_plan_revised",
                plan_id,
                {
                    "version": payload["version"],
                    "digest": digest(payload),
                    "reason": reason.strip(),
                },
            )
        return payload

    def get(self, plan_id, version=None):
        name(plan_id)
        if version is None:
            row = self.db.one(
                "SELECT payload,digest FROM analysis_plans WHERE id=? "
                "ORDER BY version DESC LIMIT 1",
                (plan_id,),
            )
        else:
            row = self.db.one(
                "SELECT payload,digest FROM analysis_plans WHERE id=? AND version=?",
                (plan_id, integer(version, 1, 1000000)),
            )
        if row is None:
            reject("missing", "analysis plan not found")
        payload = parse_json(row["payload"])
        if digest(payload) != row["digest"]:
            reject("integrity", "analysis plan digest mismatch")
        return payload

    def versions(self, plan_id):
        name(plan_id)
        result = []
        for row in self.db.rows(
            "SELECT payload,digest FROM analysis_plans WHERE id=? ORDER BY version",
            (plan_id,),
        ):
            payload = parse_json(row["payload"])
            if digest(payload) != row["digest"]:
                reject("integrity", "analysis plan digest mismatch")
            result.append(
                {
                    "id": plan_id,
                    "version": payload["version"],
                    "digest": row["digest"],
                    "parent": payload["parent"],
                    "change_reason": payload["change_reason"],
                }
            )
        if not result:
            reject("missing", "analysis plan not found")
        return result

    def _build(
        self,
        plan_id,
        version,
        parent,
        change_reason,
        title,
        assignment,
        unit,
        arm,
        strata,
        primary,
        secondary,
        cutoff,
        actual_arm_field,
        preprocessing,
        adjustment,
        permutations,
        seed,
        confidence,
    ):
        name(plan_id)
        if not isinstance(title, str) or not title.strip() or len(title) > 512:
            reject("input", "plan title must be bounded text")
        object_value(assignment, {"dataset", "version"}, {"dataset", "version"})
        assignment_dataset = name(assignment["dataset"])
        assignment_version = integer(assignment["version"], 1)
        record = self.registry.version(assignment_dataset, assignment_version)
        if record["state"] != "active":
            reject("state", "assignment version is withdrawn")
        registration = self.registry.dataset(assignment_dataset)
        schema = self.registry.schema(
            registration["schema_id"], record["schema_version"]
        )
        unit = name(unit)
        if schema.primary_key != [unit]:
            reject("plan", "experimental unit must be the assignment primary key")
        if schema.by_name[unit].type not in {"text", "integer"}:
            reject("plan", "experimental unit must be text or integer")
        object_value(arm, {"field", "control", "treatment"}, {"field", "control", "treatment"})
        arm_field = name(arm["field"])
        arm_definition = schema.by_name.get(arm_field)
        if arm_definition is None or arm_definition.type != "enum":
            reject("plan", "arm field must be an assignment enumeration")
        control = _label(arm["control"])
        treatment = _label(arm["treatment"])
        if control == treatment:
            reject("plan", "control and treatment labels must differ")
        for label in (control, treatment):
            if label not in arm_definition.choices:
                reject("plan", "arm label absent from assignment enumeration", label=label)
        strata = [name(field) for field in list_value(strata or [], 0, 8)]
        if len(set(strata)) != len(strata):
            reject("plan", "duplicate stratum field")
        for field in strata:
            if field in {unit, arm_field}:
                reject("plan", "stratum field collides with unit or arm")
            definition = schema.by_name.get(field)
            if definition is None or definition.type not in {"text", "enum", "integer"}:
                reject("plan", "stratum field must be assignment text, enum or integer")
            if definition.nullable:
                reject("plan", "stratum field must be non-nullable")
        primary = _endpoint(primary)
        secondary = [_endpoint(item) for item in list_value(secondary or [], 0, 20)]
        identifiers = [primary["id"]] + [item["id"] for item in secondary]
        if len(set(identifiers)) != len(identifiers):
            reject("plan", "endpoint identifiers must be unique")
        object_value(cutoff, {"field", "at"}, {"field", "at"})
        cutoff = {"field": name(cutoff["field"]), "at": moment(cutoff["at"])}
        actual_arm_field = name(actual_arm_field)
        preprocessing = preprocessing or {}
        object_value(preprocessing, {"repeated", "bounds"})
        repeated = preprocessing.get("repeated", "latest_before_cutoff")
        if repeated not in REPEATED_RULES:
            reject("plan", "unknown repeated measurement rule")
        bounds = preprocessing.get("bounds")
        if bounds is not None:
            object_value(bounds, {"minimum", "maximum"}, {"minimum", "maximum"})
            low, high = decimal(bounds["minimum"]), decimal(bounds["maximum"])
            if low > high:
                reject("plan", "preprocessing bounds reversed")
            bounds = {"minimum": decimal_text(low), "maximum": decimal_text(high)}
        if adjustment is not None:
            object_value(adjustment, {"covariates"}, {"covariates"})
            covariates = [
                name(field) for field in list_value(adjustment["covariates"], 1, 8)
            ]
            if len(set(covariates)) != len(covariates):
                reject("plan", "duplicate covariate")
            for field in covariates:
                if field in {unit, arm_field} or field in strata:
                    reject("plan", "covariate collides with design field")
                definition = schema.by_name.get(field)
                if definition is None or definition.type not in {"integer", "decimal"}:
                    reject(
                        "plan",
                        "covariate must be numeric pre-enrollment data",
                        field=field,
                    )
            if primary["kind"] != "continuous":
                reject(
                    "plan",
                    "covariate adjustment requires a continuous primary endpoint",
                )
            adjustment = {"covariates": covariates}
        permutations = integer(permutations, 10, 50000)
        if not isinstance(seed, str) or not seed or len(seed) > 256:
            reject("input", "plan seed must be bounded text")
        if confidence not in CONFIDENCE_Z:
            reject("plan", "unsupported confidence level")
        rows = self.ingestion.rows(assignment_dataset, assignment_version)
        if not rows:
            reject("plan", "assignment version has no enrolled units")
        arms_seen = {control: 0, treatment: 0}
        strata_enrollment = {}
        for row in rows:
            if row[arm_field] not in arms_seen:
                reject(
                    "plan",
                    "assignment row uses arm outside the planned pair",
                    unit=row[unit],
                )
            arms_seen[row[arm_field]] += 1
            key = canonical([row[field] for field in strata])
            strata_enrollment[key] = strata_enrollment.get(key, 0) + 1
        if not arms_seen[control] or not arms_seen[treatment]:
            reject("plan", "both arms must be enrolled in the locked assignment")
        if len(strata_enrollment) > 10000:
            reject("plan", "too many distinct strata")
        return {
            "schema": 1,
            "id": plan_id,
            "version": version,
            "parent": parent,
            "change_reason": change_reason,
            "title": title.strip(),
            "assignment": {
                "dataset": assignment_dataset,
                "version": assignment_version,
                "digest": record["digest"],
            },
            "unit": unit,
            "arm": {"field": arm_field, "control": control, "treatment": treatment},
            "strata": strata,
            "endpoints": {"primary": primary, "secondary": secondary},
            "cutoff": cutoff,
            "actual_arm_field": actual_arm_field,
            "preprocessing": {"repeated": repeated, "bounds": bounds},
            "adjustment": adjustment,
            "permutations": permutations,
            "seed": seed,
            "confidence": confidence,
            "enrollment": {
                "units": len(rows),
                "control": arms_seen[control],
                "treatment": arms_seen[treatment],
                "strata": [
                    {"key": dict(zip(strata, parse_json(key))), "units": count}
                    for key, count in sorted(strata_enrollment.items())
                ],
            },
        }


def _endpoint(data):
    object_value(data, {"id", "kind", "field"}, {"id", "kind", "field"})
    if data["kind"] not in ENDPOINT_KINDS:
        reject("plan", "endpoint kind must be continuous or binary")
    return {
        "id": name(data["id"]),
        "kind": data["kind"],
        "field": name(data["field"]),
    }


def _label(value):
    if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
        reject("plan", "arm label must be bounded text")
    return value


def _content(payload):
    return canonical(
        {
            key: value
            for key, value in payload.items()
            if key not in {"version", "parent", "change_reason"}
        }
    )
