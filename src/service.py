from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine, recalc_cluster


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 回执对账
    # ------------------------------------------------------------------

    def create_receipt(self, actor, data, idempotency_key=None, offline=False):
        payload = dict(data or {})
        payload["synced"] = not offline
        payload["retry_count"] = 0
        receipt = self.create(actor, "receipt", payload, idempotency_key)
        if not offline:
            receipt = self._reconcile(receipt, actor)
        return receipt

    def reconcile_receipt(self, actor, receipt_id):
        receipt = self._load_receipt(receipt_id)
        self.rules.ensure_role(actor, ("admin", "lab"))
        if receipt["status"] not in ("received", "failed"):
            raise InvalidTransition("cannot reconcile receipt in status %s" % receipt["status"])
        return self._reconcile(receipt, actor)

    def retry_receipt(self, actor, receipt_id):
        receipt = self._load_receipt(receipt_id)
        self.rules.ensure_role(actor, ("admin", "lab"))
        if receipt["status"] != "failed":
            raise InvalidTransition("can only retry failed receipts, got %s" % receipt["status"])
        return self._reconcile(receipt, actor)

    def merge_receipts(self, actor, receipt_ids=None):
        """Merge offline receipts back to the center, reconciling each one."""
        self.rules.ensure_role(actor, ("admin", "lab"))
        if receipt_ids:
            receipts = [self._load_receipt(item) for item in receipt_ids]
        else:
            receipts = [
                item
                for item in self.repository.list_entities(kind="receipt")
                if not item["data"].get("synced", True)
            ]
        results = []
        for receipt in receipts:
            if receipt["status"] in ("matched", "synced"):
                results.append({"id": receipt["id"], "status": receipt["status"], "skipped": True})
                continue
            updated = self._reconcile(receipt, actor)
            results.append({"id": updated["id"], "status": updated["status"]})
        return {"merged": results}

    def _load_receipt(self, receipt_id):
        receipt = self.repository.get_entity(receipt_id)
        if not receipt:
            raise NotFoundError("receipt not found: " + receipt_id)
        if receipt["kind"] != "receipt":
            raise InvalidTransition("entity %s is not a receipt" % receipt_id)
        return receipt

    def _set_receipt_status(self, receipt, status, actor, synced=None, **extra):
        data = dict(receipt["data"])
        if synced is not None:
            data["synced"] = synced
        data.update(extra)
        if status == "failed":
            data["retry_count"] = int(data.get("retry_count", 0)) + 1
        updated = self.repository.update_entity(receipt["id"], receipt["version"], status, data)
        self.audit.record(
            receipt["id"], actor, "reconcile", receipt["status"], status, dict(extra)
        )
        return updated

    def _reconcile(self, receipt, actor):
        data = dict(receipt["data"])
        batch = data.get("batch_code")
        sample_code = data.get("sample_code")
        lab_id = data.get("lab_id")
        result = data.get("result")

        # 1) 按批次和样本编号去重：已对账的同批次同样本回执不再重复入账
        duplicates = self.repository.find_entities_by_fields(
            "receipt", {"batch_code": batch, "sample_code": sample_code}
        )
        for dup in duplicates:
            if dup["id"] == receipt["id"] or dup["status"] != "matched":
                continue
            if dup["data"].get("result") == result:
                return self._set_receipt_status(
                    receipt, "matched", actor, synced=True, duplicate_of=dup["id"]
                )
            return self._set_receipt_status(
                receipt,
                "suspended",
                actor,
                synced=True,
                reason="conflicting result for batch %s sample %s" % (batch, sample_code),
            )

        # 2) 样本对不上：先挂起，不改样本状态
        samples = self.repository.find_entities("sample", "sample_code", sample_code)
        sample = samples[0] if samples else None
        if not sample:
            return self._set_receipt_status(
                receipt,
                "suspended",
                actor,
                synced=True,
                reason="sample not found: " + str(sample_code),
            )

        # 3) 实验室越权：样本送检实验室与回执实验室不一致，直接拒绝
        sample_lab = sample["data"].get("lab_id")
        if sample_lab is not None and sample_lab != lab_id:
            return self._set_receipt_status(
                receipt,
                "rejected",
                actor,
                synced=True,
                reason="lab mismatch: sample sent to %s, receipt from %s"
                % (sample_lab, lab_id),
            )

        # 4) 样本已在实验室：应用结论并重算聚集事件
        if sample["status"] == "in_lab":
            try:
                updated_sample = self._apply_result(sample, receipt, actor)
            except Exception as exc:  # 入库失败：留成待重试项，样本状态不动
                return self._set_receipt_status(
                    receipt, "failed", actor, synced=True, reason=str(exc)
                )
            self._invalidate_and_recalc(updated_sample, actor)
            return self._set_receipt_status(
                receipt,
                "matched",
                actor,
                synced=True,
                sample_id=updated_sample["id"],
            )

        # 5) 样本已有结论：同结论按去重处理，冲突结论挂起
        if sample["status"] in ("resulted", "closed"):
            if sample["data"].get("result") == result:
                return self._set_receipt_status(
                    receipt,
                    "matched",
                    actor,
                    synced=True,
                    sample_id=sample["id"],
                    duplicate=True,
                )
            return self._set_receipt_status(
                receipt,
                "suspended",
                actor,
                synced=True,
                reason="conflicting result for already-resulted sample %s" % sample_code,
            )

        # 6) 样本尚未送检
        return self._set_receipt_status(
            receipt,
            "suspended",
            actor,
            synced=True,
            reason="sample not yet in lab (status %s)" % sample["status"],
        )

    def _apply_result(self, sample, receipt, actor):
        patch = {
            "result": receipt["data"]["result"],
            "result_at": receipt["data"]["result_at"],
            "receipt_id": receipt["id"],
        }
        merged = dict(sample["data"])
        merged.update(patch)
        updated = self.repository.update_entity(
            sample["id"], sample["version"], "resulted", merged
        )
        self.audit.record(
            sample["id"],
            actor,
            "lab_result",
            sample["status"],
            "resulted",
            {"receipt_id": receipt["id"], "result": patch["result"]},
        )
        return updated

    def _clusters_referencing_sample(self, sample):
        obs_id = sample["data"].get("observation_id")
        if not obs_id:
            return []
        return [
            cluster
            for cluster in self.repository.list_entities(kind="cluster")
            if obs_id in cluster["data"].get("observation_ids", [])
        ]

    def _invalidate_and_recalc(self, sample, actor):
        """样本结论一变，引用它的聚集事件失效并重算成员。"""
        for cluster in self._clusters_referencing_sample(sample):
            obs_ids = cluster["data"].get("observation_ids", [])
            observations = []
            samples_by_observation = {}
            for oid in obs_ids:
                obs = self.repository.get_entity(oid)
                if not obs:
                    continue
                observations.append(obs)
                samples_by_observation[oid] = self.repository.find_entities(
                    "sample", "observation_id", oid
                )
            still_cluster, members = recalc_cluster(
                cluster, observations, samples_by_observation
            )
            data = dict(cluster["data"])
            data["members"] = members
            data["still_cluster"] = still_cluster
            updated = self._update_cluster_status(cluster, "invalid", data)
            self.audit.record(
                updated["id"],
                actor,
                "recalc_members",
                cluster["status"],
                "invalid",
                {"still_cluster": still_cluster, "members": members},
            )

    def _update_cluster_status(self, cluster, status, data):
        try:
            return self.repository.update_entity(cluster["id"], cluster["version"], status, data)
        except ConflictError:
            fresh = self.repository.get_entity(cluster["id"])
            if not fresh:
                raise
            return self.repository.update_entity(fresh["id"], fresh["version"], status, data)
