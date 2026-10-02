import sqlite3
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
)
from .repository import utcnow
from .rules import recompute_cluster_members


class ReconciliationService:
    """Reconcile lab receipts against dispatch orders, samples and clusters.

    A receipt carries lab_id, batch_no and per-sample items. Items are
    matched against the lab_order ledger for the batch: unmatched items are
    suspended without touching samples, unauthorized labs are rejected, and
    applied items are deduplicated by (batch_no, sample_code) so a resent
    receipt never records a result twice. Items that fail to persist stay
    as pending_retry; offline receipts wait in outbox until sync merges
    them back into the center ledger.
    """

    PROCESSABLE_STATES = ("received", "pending_retry", "outbox")

    def __init__(self, repository, rules, audit=None):
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditTrail(repository)

    # ---- ingest -------------------------------------------------------

    def ingest_receipt(self, actor, payload, idempotency_key=None, offline=False):
        data = dict(payload or {})
        data.pop("mode", None)
        lab_id = data.get("lab_id")
        if actor.role == "lab" and actor.user_id != lab_id:
            raise PermissionDenied(
                "lab %s cannot post receipts for %s" % (actor.user_id, lab_id)
            )
        self.rules.validate_create(actor, "receipt", data)
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        receipt_id = str(data.pop("id", "") or uuid4())
        if self.repository.get_entity(receipt_id):
            raise ConflictError("entity already exists: " + receipt_id)
        items = data.get("items") or []
        status = "outbox" if offline else "received"
        data["origin"] = "offline" if offline else "center"
        data["summary"] = {}
        entity = self.repository.create_entity(
            receipt_id, "receipt", status, data, actor.user_id
        )
        for item in items:
            self.repository.add_receipt_item(
                receipt_id,
                lab_id,
                data.get("batch_no"),
                item.get("sample_code"),
                item.get("result"),
                item.get("result_at"),
                status,
            )
        self.audit.record(
            receipt_id,
            actor,
            "ingest_offline" if offline else "ingest",
            None,
            status,
            {"lab_id": lab_id, "batch_no": data.get("batch_no"), "item_count": len(items)},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, receipt_id)
        if offline:
            return entity
        return self.reconcile(receipt_id, actor)

    # ---- reconcile ----------------------------------------------------

    def reconcile(self, receipt_id, actor):
        receipt = self.repository.get_entity(receipt_id)
        if not receipt or receipt["kind"] != "receipt":
            raise NotFoundError("receipt not found: " + str(receipt_id))
        data = receipt["data"]
        batch_no = data.get("batch_no")
        order = self._find_order(batch_no)
        items = [
            item
            for item in self.repository.list_receipt_items(receipt_id=receipt_id)
            if item["state"] in self.PROCESSABLE_STATES
        ]
        if order and order["data"].get("lab_id") != data.get("lab_id"):
            reason = "lab %s not authorized for batch %s" % (
                data.get("lab_id"),
                batch_no,
            )
            for item in items:
                self.repository.update_receipt_item(item["id"], "rejected", reason=reason)
            return self._finish(receipt, actor, forced_status="rejected", note=reason)
        if not order:
            reason = "unknown batch_no: " + str(batch_no)
            for item in items:
                self.repository.update_receipt_item(item["id"], "suspended", reason=reason)
            return self._finish(receipt, actor, forced_status="suspended", note=reason)
        for item in items:
            self._reconcile_item(receipt, order, item, actor)
        return self._finish(receipt, actor)

    def retry_receipt(self, receipt_id, actor):
        receipt = self.repository.get_entity(receipt_id)
        if not receipt or receipt["kind"] != "receipt":
            raise NotFoundError("receipt not found: " + str(receipt_id))
        self._ensure_receipt_actor(actor, receipt)
        return self.reconcile(receipt_id, actor)

    def sync(self, actor):
        if actor.role not in ("admin", "lab"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        merged = []
        for receipt in self.repository.list_entities(kind="receipt", status="outbox"):
            if actor.role == "lab" and receipt["data"].get("lab_id") != actor.user_id:
                continue
            merged.append(self.reconcile(receipt["id"], actor))
        retried = []
        for receipt in self.repository.list_entities(kind="receipt", status="pending_retry"):
            if actor.role == "lab" and receipt["data"].get("lab_id") != actor.user_id:
                continue
            retried.append(self.reconcile(receipt["id"], actor))
        return {
            "merged": [self._brief(receipt) for receipt in merged],
            "retried": [self._brief(receipt) for receipt in retried],
        }

    # ---- items --------------------------------------------------------

    def _reconcile_item(self, receipt, order, item, actor):
        data = receipt["data"]
        batch_no = data.get("batch_no")
        lab_id = data.get("lab_id")
        code = item["sample_code"]
        applied = self.repository.find_applied_receipt_item(batch_no, code)
        if applied:
            self.repository.update_receipt_item(
                item["id"],
                "duplicate",
                reason="already applied by receipt " + applied["receipt_id"],
            )
            return
        if code not in (order["data"].get("sample_codes") or []):
            self.repository.update_receipt_item(
                item["id"], "suspended", reason="sample not in dispatch batch"
            )
            return
        sample = self._find_sample(code)
        if not sample:
            self.repository.update_receipt_item(
                item["id"], "suspended", reason="unknown sample_code"
            )
            return
        sample_lab = sample["data"].get("lab_id")
        if sample_lab and sample_lab != lab_id:
            self.repository.update_receipt_item(
                item["id"],
                "rejected",
                reason="sample assigned to lab " + str(sample_lab),
            )
            return
        if sample["status"] == "closed":
            self.repository.update_receipt_item(
                item["id"], "suspended", reason="sample closed"
            )
            return
        if sample["status"] not in ("in_lab", "resulted"):
            self.repository.update_receipt_item(
                item["id"], "suspended", reason="sample not dispatched to lab"
            )
            return
        try:
            self._apply_result(receipt, sample, item, actor)
            self.repository.update_receipt_item(
                item["id"], "applied", sample_id=sample["id"]
            )
        except sqlite3.IntegrityError:
            self.repository.update_receipt_item(
                item["id"], "duplicate", reason="already applied for this batch"
            )
        except Exception as exc:
            self.repository.update_receipt_item(
                item["id"], "pending_retry", reason=type(exc).__name__ + ": " + str(exc)
            )

    def _apply_result(self, receipt, sample, item, actor):
        data = receipt["data"]
        lab_id = data.get("lab_id")
        new_result = str(item.get("result")).lower()
        status = sample["status"]
        previous = sample["data"].get("result")
        if status == "resulted" and previous and str(previous).lower() == new_result:
            return  # same conclusion already recorded, nothing to change
        merged = dict(sample["data"])
        merged.update(
            {
                "result": new_result,
                "result_at": item.get("result_at") or utcnow(),
                "lab_id": lab_id,
                "receipt_id": receipt["id"],
                "batch_no": data.get("batch_no"),
                "result_source": "receipt",
            }
        )
        updated = self.repository.update_entity(
            sample["id"], sample["version"], "resulted", merged
        )
        self.audit.record(
            sample["id"],
            actor,
            "lab_result",
            status,
            "resulted",
            {
                "receipt_id": receipt["id"],
                "batch_no": data.get("batch_no"),
                "sample_code": item["sample_code"],
                "previous_result": previous,
            },
        )
        if previous and str(previous).lower() != new_result:
            self.cascade_sample_change(updated, actor)

    # ---- cluster cascade ----------------------------------------------

    def cascade_sample_change(self, sample, actor):
        """Invalidate clusters referencing a sample whose conclusion changed
        and recompute their members."""
        observation_id = sample["data"].get("observation_id")
        affected = [
            cluster
            for cluster in self.repository.list_entities(kind="cluster")
            if cluster["status"] in ("draft", "confirmed")
            and (
                observation_id in (cluster["data"].get("observation_ids") or [])
                or sample["id"] in (cluster["data"].get("sample_ids") or [])
            )
        ]
        if not affected:
            return
        observations = self.repository.list_entities(kind="observation")
        samples = self.repository.list_entities(kind="sample")
        for cluster in affected:
            data = dict(cluster["data"])
            data["previous_observation_ids"] = cluster["data"].get("observation_ids")
            data["invalidated_reason"] = "sample %s conclusion changed" % sample[
                "data"
            ].get("sample_code")
            invalidated = self.repository.update_entity(
                cluster["id"], cluster["version"], "invalidated", data
            )
            self.audit.record(
                cluster["id"],
                actor,
                "invalidate",
                cluster["status"],
                "invalidated",
                {"sample_id": sample["id"], "sample_code": sample["data"].get("sample_code")},
            )
            member_ids, qualifies = recompute_cluster_members(
                data, observations, samples
            )
            data["observation_ids"] = member_ids
            data["recomputed_at"] = utcnow()
            next_status = "confirmed" if qualifies else "invalidated"
            if not qualifies:
                data["invalidated_reason"] += "; recomputed members < 3"
            self.repository.update_entity(
                cluster["id"], invalidated["version"], next_status, data
            )
            self.audit.record(
                cluster["id"],
                actor,
                "recompute",
                "invalidated",
                next_status,
                {"observation_ids": member_ids},
            )

    # ---- helpers ------------------------------------------------------

    def _finish(self, receipt, actor, forced_status=None, note=None):
        items = self.repository.list_receipt_items(receipt_id=receipt["id"])
        summary = {}
        for item in items:
            summary[item["state"]] = summary.get(item["state"], 0) + 1
        status = forced_status or self._summarize(summary)
        data = dict(receipt["data"])
        data["summary"] = summary
        if note:
            data["note"] = note
        updated = self.repository.update_entity(
            receipt["id"], receipt["version"], status, data
        )
        self.audit.record(
            receipt["id"], actor, "reconcile", receipt["status"], status, {"summary": summary}
        )
        return updated

    @staticmethod
    def _summarize(summary):
        if summary.get("pending_retry"):
            return "pending_retry"
        states = {state for state in summary if state != "received"}
        if not states:
            return "received"
        if states <= {"applied", "duplicate"}:
            return "applied"
        if states == {"suspended"}:
            return "suspended"
        if states == {"rejected"}:
            return "rejected"
        return "partial"

    @staticmethod
    def _brief(receipt):
        return {
            "id": receipt["id"],
            "status": receipt["status"],
            "summary": receipt["data"].get("summary", {}),
        }

    def _find_order(self, batch_no):
        rows = self.repository.find_entities("lab_order", "batch_no", batch_no)
        return rows[0] if rows else None

    def _find_sample(self, sample_code):
        rows = self.repository.find_entities("sample", "sample_code", sample_code)
        return rows[0] if rows else None

    @staticmethod
    def _ensure_receipt_actor(actor, receipt):
        if actor.role == "lab" and receipt["data"].get("lab_id") != actor.user_id:
            raise PermissionDenied(
                "lab %s cannot touch receipts for %s"
                % (actor.user_id, receipt["data"].get("lab_id"))
            )
