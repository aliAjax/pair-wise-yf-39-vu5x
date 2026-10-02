import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.reconcile import ReconciliationService
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.rules = RuleEngine()
        self.service = DomainService(self.repo, self.rules)
        self.reconcile = ReconciliationService(self.repo, self.rules)
        self.service.sample_result_listeners.append(self.reconcile.cascade_sample_change)
        self.admin = Actor("admin", "admin")
        self.epi = Actor("epi", "epidemiologist")
        self.lab1 = Actor("LAB-1", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _observation(self, event_id, day, lat=40.0, lon=116.0):
        obs = self.service.create(self.admin, "observation", {
            "event_id": event_id, "species": "deer", "location": "North",
            "observed_at": "2026-04-%02d" % day, "lat": lat, "lon": lon,
        })
        return self.service.transition(self.admin, obs["id"], "submit", {
            "location": "North", "observed_at": "2026-04-%02d" % day,
        })

    def _sample_in_lab(self, code, obs_id, lab_id="LAB-1"):
        sample = self.service.create(self.admin, "sample", {
            "observation_id": obs_id, "sample_code": code,
        })
        return self.service.transition(
            self.admin, sample["id"], "send_lab", {"lab_id": lab_id}
        )

    def _order(self, batch, codes, lab_id="LAB-1"):
        return self.service.create(self.admin, "lab_order", {
            "lab_id": lab_id, "batch_no": batch, "sample_codes": codes,
        })

    def _receipt(self, batch, items, lab_id="LAB-1", actor=None, **kwargs):
        return self.reconcile.ingest_receipt(actor or self.lab1, {
            "lab_id": lab_id, "batch_no": batch, "items": items,
        }, **kwargs)

    def test_receipt_applies_results(self):
        obs = self._observation("E-1", 1)
        sample = self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"])
        receipt = self._receipt("B-1", [
            {"sample_code": "W-1", "result": "positive", "result_at": "2026-04-02"},
        ])
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(receipt["data"]["summary"], {"applied": 1})
        updated = self.service.get(sample["id"])
        self.assertEqual(updated["status"], "resulted")
        self.assertEqual(updated["data"]["result"], "positive")
        self.assertEqual(updated["data"]["batch_no"], "B-1")
        items = self.repo.list_receipt_items(receipt_id=receipt["id"])
        self.assertEqual(items[0]["state"], "applied")
        self.assertEqual(items[0]["sample_id"], sample["id"])

    def test_resent_receipt_does_not_double_record(self):
        obs = self._observation("E-1", 1)
        sample = self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"])
        self._receipt("B-1", [{"sample_code": "W-1", "result": "positive"}])
        version_after_first = self.service.get(sample["id"])["version"]
        second = self._receipt("B-1", [{"sample_code": "W-1", "result": "positive"}])
        self.assertEqual(second["status"], "applied")
        self.assertEqual(second["data"]["summary"], {"duplicate": 1})
        self.assertEqual(self.service.get(sample["id"])["version"], version_after_first)
        applied = self.repo.list_receipt_items(state="applied", batch_no="B-1")
        self.assertEqual(len(applied), 1)

    def test_unmatched_items_are_suspended_without_touching_samples(self):
        obs = self._observation("E-1", 1)
        sample = self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"])
        receipt = self._receipt("B-1", [
            {"sample_code": "W-1", "result": "positive"},
            {"sample_code": "W-UNKNOWN", "result": "negative"},
        ])
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["data"]["summary"], {"applied": 1, "suspended": 1})
        other = self._sample_in_lab("W-2", obs["id"])
        receipt2 = self._receipt("B-1", [{"sample_code": "W-2", "result": "positive"}])
        self.assertEqual(receipt2["status"], "suspended")
        self.assertEqual(self.service.get(other["id"])["status"], "in_lab")
        receipt3 = self._receipt("B-NONE", [{"sample_code": "W-1", "result": "negative"}])
        self.assertEqual(receipt3["status"], "suspended")
        self.assertEqual(self.service.get(sample["id"])["data"]["result"], "positive")

    def test_unauthorized_lab_receipt_is_rejected(self):
        obs = self._observation("E-1", 1)
        sample = self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"], lab_id="LAB-1")
        receipt = self._receipt(
            "B-1",
            [{"sample_code": "W-1", "result": "positive"}],
            lab_id="LAB-2",
            actor=Actor("LAB-2", "lab"),
        )
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(self.service.get(sample["id"])["status"], "in_lab")
        items = self.repo.list_receipt_items(receipt_id=receipt["id"])
        self.assertEqual(items[0]["state"], "rejected")

    def test_lab_cannot_post_for_another_lab(self):
        with self.assertRaises(PermissionDenied):
            self._receipt(
                "B-1",
                [{"sample_code": "W-1", "result": "positive"}],
                lab_id="LAB-9",
                actor=Actor("LAB-2", "lab"),
            )

    def test_conclusion_change_invalidates_and_recomputes_cluster(self):
        observations = [
            self._observation("E-%d" % i, i, lat=40.0 + i * 0.005, lon=116.0 + i * 0.005)
            for i in range(1, 5)
        ]
        self._order("B-1", ["W-1", "W-2", "W-3", "W-4"])
        for i, obs in enumerate(observations, start=1):
            self._sample_in_lab("W-%d" % i, obs["id"])
        self._receipt("B-1", [
            {"sample_code": "W-%d" % i, "result": "positive"} for i in range(1, 5)
        ])
        cluster = self.service.create(self.epi, "cluster", {"region": "North"})
        cluster = self.service.transition(self.epi, cluster["id"], "confirm_cluster", {
            "observation_ids": [obs["id"] for obs in observations],
            "centroid": [40.01, 116.01],
        })
        self.assertEqual(cluster["status"], "confirmed")
        self._order("B-2", ["W-1"])
        self._receipt("B-2", [{"sample_code": "W-1", "result": "negative"}])
        updated = self.service.get(cluster["id"])
        self.assertEqual(updated["status"], "confirmed")
        self.assertEqual(len(updated["data"]["observation_ids"]), 3)
        self.assertNotIn(observations[0]["id"], updated["data"]["observation_ids"])
        self.assertEqual(len(updated["data"]["previous_observation_ids"]), 4)
        self._order("B-3", ["W-2"])
        self._receipt("B-3", [{"sample_code": "W-2", "result": "negative"}])
        updated = self.service.get(cluster["id"])
        self.assertEqual(updated["status"], "invalidated")
        self.assertEqual(len(updated["data"]["observation_ids"]), 2)

    def test_manual_result_change_cascades_to_cluster(self):
        observations = [
            self._observation("E-%d" % i, i, lat=40.0 + i * 0.005, lon=116.0 + i * 0.005)
            for i in range(1, 4)
        ]
        samples = [
            self._sample_in_lab("W-%d" % i, observations[i - 1]["id"])
            for i in range(1, 4)
        ]
        self._order("B-1", ["W-1", "W-2", "W-3"])
        self._receipt("B-1", [
            {"sample_code": "W-%d" % i, "result": "positive"} for i in range(1, 4)
        ])
        cluster = self.service.create(self.epi, "cluster", {"region": "North"})
        cluster = self.service.transition(self.epi, cluster["id"], "confirm_cluster", {
            "observation_ids": [obs["id"] for obs in observations],
            "centroid": [40.01, 116.01],
        })
        self.service.transition(self.lab1, samples[0]["id"], "retest", {"reason": "qc"})
        self.service.transition(self.lab1, samples[0]["id"], "lab_result", {
            "result": "negative", "result_at": "2026-04-06",
        })
        updated = self.service.get(cluster["id"])
        self.assertEqual(updated["status"], "invalidated")
        self.assertEqual(len(updated["data"]["observation_ids"]), 2)

    def test_failed_persistence_becomes_pending_retry(self):
        obs = self._observation("E-1", 1)
        sample = self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"])
        original = self.repo.update_entity
        failed = {"done": False}

        def flaky(entity_id, *args, **kwargs):
            if entity_id == sample["id"] and not failed["done"]:
                failed["done"] = True
                raise ConflictError("simulated storage failure")
            return original(entity_id, *args, **kwargs)

        self.repo.update_entity = flaky
        receipt = self._receipt("B-1", [{"sample_code": "W-1", "result": "positive"}])
        self.assertEqual(receipt["status"], "pending_retry")
        self.assertEqual(self.service.get(sample["id"])["status"], "in_lab")
        items = self.repo.list_receipt_items(receipt_id=receipt["id"])
        self.assertEqual(items[0]["state"], "pending_retry")
        self.assertIn("simulated storage failure", items[0]["reason"])
        retried = self.reconcile.retry_receipt(receipt["id"], self.lab1)
        self.assertEqual(retried["status"], "applied")
        self.assertEqual(self.service.get(sample["id"])["status"], "resulted")

    def test_offline_receipts_merge_on_sync(self):
        obs = self._observation("E-1", 1)
        sample = self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"])
        receipt = self._receipt(
            "B-1", [{"sample_code": "W-1", "result": "positive"}], offline=True
        )
        self.assertEqual(receipt["status"], "outbox")
        self.assertEqual(self.service.get(sample["id"])["status"], "in_lab")
        result = self.reconcile.sync(self.admin)
        self.assertEqual([item["id"] for item in result["merged"]], [receipt["id"]])
        self.assertEqual(result["merged"][0]["status"], "applied")
        self.assertEqual(self.service.get(sample["id"])["status"], "resulted")
        version = self.service.get(sample["id"])["version"]
        again = self.reconcile.sync(self.admin)
        self.assertEqual(again["merged"], [])
        self.assertEqual(again["retried"], [])
        self.assertEqual(self.service.get(sample["id"])["version"], version)

    def test_ingest_idempotency_key_returns_same_receipt(self):
        obs = self._observation("E-1", 1)
        self._sample_in_lab("W-1", obs["id"])
        self._order("B-1", ["W-1"])
        payload = {
            "lab_id": "LAB-1", "batch_no": "B-1",
            "items": [{"sample_code": "W-1", "result": "positive"}],
        }
        first = self.reconcile.ingest_receipt(self.lab1, payload, idempotency_key="rcpt-1")
        second = self.reconcile.ingest_receipt(self.lab1, payload, idempotency_key="rcpt-1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.repo.list_receipt_items(receipt_id=first["id"])), 1)

    def test_lab_order_rules(self):
        order = self._order("B-1", ["W-1"])
        with self.assertRaises(ConflictError):
            self._order("B-1", ["W-2"])
        closed = self.service.transition(self.admin, order["id"], "close_order", {})
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("LAB-1", "lab"), "lab_order", {
                "lab_id": "LAB-1", "batch_no": "B-2", "sample_codes": ["W-9"],
            })


if __name__ == "__main__":
    unittest.main()
