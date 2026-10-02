from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_observation(actor, data, lookup):
    rows = lookup("observation", "event_id", data.get("event_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("observed_at") == data.get("observed_at"):
            raise ConflictError("duplicate observation event")
    if not data.get("species"):
        raise ValidationError("species is required")


def _validate_sample(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] not in ("submitted", "sampled"):
        raise ValidationError("sample requires a submitted observation")


def validate_result_value(result):
    if str(result or "").lower() not in ("positive", "negative"):
        raise ValidationError("lab result must be positive or negative")


def _validate_lab_result(actor, entity, data, lookup):
    validate_result_value(data.get("result"))


def _validate_lab_order(actor, data, lookup):
    codes = data.get("sample_codes")
    if not isinstance(codes, list) or not codes:
        raise ValidationError("sample_codes must be a non-empty list")
    rows = lookup("lab_order", "batch_no", data.get("batch_no")) if lookup else []
    if rows:
        raise ConflictError("duplicate batch_no: " + str(data.get("batch_no")))


def _validate_receipt(actor, data, lookup):
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("items must be a non-empty list")
    for item in items:
        if not isinstance(item, dict) or not item.get("sample_code"):
            raise ValidationError("receipt item requires sample_code")
        validate_result_value(item.get("result"))


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def is_cluster(observations, max_days=14, radius_km=10):
    if len(observations) < 3:
        return False
    points = observations[:3]
    same_window = all(
        abs(_date_ordinal(points[0].get("observed_at")) - _date_ordinal(item.get("observed_at"))) <= max_days
        for item in points[1:]
    )
    close = all(
        _haversine_km(points[0]["lat"], points[0]["lon"], item["lat"], item["lon"]) <= radius_km
        for item in points[1:]
    )
    return same_window and close


def recompute_cluster_members(cluster_data, observations, samples, max_days=14, radius_km=10):
    """Recompute which observations still qualify as cluster members.

    Members are observations in the cluster region with at least one
    positive sample, inside the centroid radius and the time window
    anchored at the earliest candidate. Returns (member_ids, qualifies).
    """
    positive_obs_ids = {
        sample["data"].get("observation_id")
        for sample in samples
        if str(sample["data"].get("result", "")).lower() == "positive"
    }
    region = cluster_data.get("region")
    centroid = cluster_data.get("centroid") or []
    candidates = []
    for obs in observations:
        if obs["id"] not in positive_obs_ids:
            continue
        if obs["status"] not in ("submitted", "sampled"):
            continue
        if region and obs["data"].get("location") != region:
            continue
        if len(centroid) == 2 and _haversine_km(
            centroid[0], centroid[1], obs["data"].get("lat"), obs["data"].get("lon")
        ) > radius_km:
            continue
        candidates.append(obs)
    candidates.sort(key=lambda obs: str(obs["data"].get("observed_at") or ""))
    if not candidates:
        return [], False
    anchor = _date_ordinal(candidates[0]["data"].get("observed_at"))
    members = [
        obs["id"]
        for obs in candidates
        if abs(_date_ordinal(obs["data"].get("observed_at")) - anchor) <= max_days
    ]
    return members, len(members) >= 3


CUSTOM_CREATE = {'observation': _validate_observation, 'sample': _validate_sample, 'lab_order': _validate_lab_order, 'receipt': _validate_receipt}
CUSTOM_TRANSITIONS = {('sample', 'lab_result'): _validate_lab_result}


class RuleEngine:
    ALIASES = {'observations': 'observation', 'samples': 'sample', 'clusters': 'cluster', 'lab_orders': 'lab_order', 'receipts': 'receipt'}
    INITIAL_STATUS = {'observation': 'captured', 'sample': 'collected', 'cluster': 'draft', 'lab_order': 'dispatched', 'receipt': 'received'}
    TRANSITIONS = {'observation': {'submit': (('captured',), 'submitted'), 'reject': (('submitted',), 'rejected'), 'link_sample': (('submitted',), 'sampled')}, 'sample': {'send_lab': (('collected',), 'in_lab'), 'lab_result': (('in_lab',), 'resulted'), 'retest': (('resulted',), 'in_lab'), 'close': (('resulted',), 'closed')}, 'cluster': {'confirm_cluster': (('draft',), 'confirmed'), 'dismiss': (('draft', 'invalidated'), 'dismissed')}, 'lab_order': {'close_order': (('dispatched',), 'closed')}}
    CREATE_REQUIRED = {'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'), 'sample': ('observation_id', 'sample_code'), 'cluster': ('region',), 'lab_order': ('lab_id', 'batch_no', 'sample_codes'), 'receipt': ('lab_id', 'batch_no', 'items')}
    ACTION_REQUIRED = {('observation', 'submit'): ('location', 'observed_at'), ('observation', 'reject'): ('reason',), ('observation', 'link_sample'): ('sample_id',), ('sample', 'send_lab'): ('lab_id',), ('sample', 'lab_result'): ('result', 'result_at'), ('sample', 'retest'): ('reason',), ('sample', 'close'): ('outcome',), ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'), ('cluster', 'dismiss'): ('reason',)}
    CREATE_ROLES = {'observation': ('admin', 'field'), 'sample': ('admin', 'field'), 'cluster': ('admin', 'epidemiologist'), 'lab_order': ('admin', 'epidemiologist'), 'receipt': ('admin', 'lab')}
    ROLE_ACTIONS = {'submit': ('admin', 'field'), 'reject': ('admin', 'epidemiologist'), 'link_sample': ('admin', 'field'), 'send_lab': ('admin', 'field'), 'lab_result': ('admin', 'lab'), 'retest': ('admin', 'lab'), 'close': ('admin', 'epidemiologist'), 'confirm_cluster': ('admin', 'epidemiologist'), 'dismiss': ('admin', 'epidemiologist'), 'close_order': ('admin', 'epidemiologist')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
