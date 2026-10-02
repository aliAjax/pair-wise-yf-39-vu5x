import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _build_sample(self, sample_code, lab_id="LAB-1"):
        obs = self.service.create(
            self.admin,
            "observation",
            {
                "event_id": "E-" + sample_code,
                "species": "deer",
                "location": "North",
                "observed_at": "2026-04-01",
                "lat": 40.0,
                "lon": 116.0,
            },
        )
        self.service.transition(
            self.admin, obs["id"], "submit", {"location": "North", "observed_at": "2026-04-01"}
        )
        sample = self.service.create(
            self.admin, "sample", {"observation_id": obs["id"], "sample_code": sample_code}
        )
        self.service.transition(self.admin, sample["id"], "send_lab", {"lab_id": lab_id})
        return obs, sample

    def _build_cluster(self):
        obs_ids = []
        sample_codes = []
        for i, (day, lat, lon) in enumerate(
            [("01", 40.0, 116.0), ("03", 40.01, 116.01), ("05", 40.02, 116.02)]
        ):
            obs = self.service.create(
                self.admin,
                "observation",
                {
                    "event_id": "E-C%d" % i,
                    "species": "deer",
                    "location": "North",
                    "observed_at": "2026-04-" + day,
                    "lat": lat,
                    "lon": lon,
                },
            )
            self.service.transition(
                self.admin,
                obs["id"],
                "submit",
                {"location": "North", "observed_at": "2026-04-" + day},
            )
            code = "W-C%d" % i
            sample = self.service.create(
                self.admin, "sample", {"observation_id": obs["id"], "sample_code": code}
            )
            self.service.transition(self.admin, sample["id"], "send_lab", {"lab_id": "LAB-1"})
            obs_ids.append(obs["id"])
            sample_codes.append(code)
        cluster = self.service.create(self.admin, "cluster", {"region": "North"})
        self.service.transition(
            self.admin,
            cluster["id"],
            "confirm_cluster",
            {"observation_ids": obs_ids, "centroid": [40.01, 116.01]},
        )
        return cluster, obs_ids, sample_codes

    def _receipt(self, batch, sample_code, result="positive", lab_id="LAB-1"):
        return self.service.create_receipt(
            self.lab,
            {
                "lab_id": lab_id,
                "batch_code": batch,
                "sample_code": sample_code,
                "result": result,
                "result_at": "2026-04-02",
            },
        )

    def test_receipt_matches_and_recalcs_cluster(self):
        cluster, obs_ids, _ = self._build_cluster()
        receipt = self._receipt("B-1", "W-C0")
        self.assertEqual(receipt["status"], "matched")

        sample = self.service.list("sample", status="resulted")[0]
        self.assertEqual(sample["data"]["result"], "positive")

        cluster = self.service.get(cluster["id"])
        self.assertEqual(cluster["status"], "invalid")
        members = cluster["data"]["members"]
        self.assertEqual(len(members), 3)
        self.assertEqual(members[0]["result"], "positive")

        # re-confirm after invalidation
        self.service.transition(
            self.admin,
            cluster["id"],
            "confirm_cluster",
            {"observation_ids": obs_ids, "centroid": [40.01, 116.01]},
        )
        self.assertEqual(self.service.get(cluster["id"])["status"], "confirmed")

        # retest the sample, then a new receipt changes the conclusion
        sample = self.service.list("sample", status="resulted")[0]
        self.service.transition(self.admin, sample["id"], "retest", {"reason": "recheck"})
        receipt2 = self._receipt("B-2", "W-C0", result="negative")
        self.assertEqual(receipt2["status"], "matched")

        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["result"], "negative")
        cluster = self.service.get(cluster["id"])
        self.assertEqual(cluster["status"], "invalid")
        w0 = [m for m in cluster["data"]["members"] if m["observation_id"] == obs_ids[0]][0]
        self.assertEqual(w0["result"], "negative")

    def test_receipt_suspended_when_sample_not_found(self):
        receipt = self._receipt("B-1", "UNKNOWN")
        self.assertEqual(receipt["status"], "suspended")
        self.assertIn("sample not found", receipt["data"]["reason"])

    def test_receipt_rejected_on_lab_mismatch(self):
        _, sample = self._build_sample("W-1", lab_id="LAB-1")
        receipt = self._receipt("B-1", "W-1", lab_id="LAB-2")
        self.assertEqual(receipt["status"], "rejected")
        self.assertIn("lab mismatch", receipt["data"]["reason"])
        # sample status untouched
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "in_lab")

    def test_receipt_rejected_on_lab_mismatch_after_result(self):
        _, sample = self._build_sample("W-1", lab_id="LAB-1")
        first = self._receipt("B-1", "W-1", lab_id="LAB-1")
        self.assertEqual(first["status"], "matched")
        # a different lab re-sending for an already-resulted sample is still overreach
        rogue = self._receipt("B-2", "W-1", lab_id="LAB-2")
        self.assertEqual(rogue["status"], "rejected")
        self.assertIn("lab mismatch", rogue["data"]["reason"])
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["result"], "positive")

    def test_receipt_dedupes_by_batch_and_sample(self):
        _, sample = self._build_sample("W-1")
        first = self._receipt("B-1", "W-1")
        self.assertEqual(first["status"], "matched")
        second = self._receipt("B-1", "W-1")
        self.assertEqual(second["status"], "matched")
        self.assertTrue(second["data"].get("duplicate_of"))
        # sample still recorded once
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "resulted")
        self.assertEqual(sample["data"]["result"], "positive")
        actions = [a["action"] for a in self.service.audit_log(entity_id=sample["id"])]
        self.assertEqual(actions.count("lab_result"), 1)

    def test_receipt_failed_then_retried(self):
        _, sample = self._build_sample("W-1")
        sample_id = sample["id"]
        original = self.repo.update_entity

        def fail(entity_id, *args, **kwargs):
            if entity_id == sample_id:
                raise RuntimeError("storage down")
            return original(entity_id, *args, **kwargs)

        self.repo.update_entity = fail
        receipt = self._receipt("B-1", "W-1")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["data"]["retry_count"], 1)
        # sample status unchanged while receipt is pending retry
        self.assertEqual(self.service.get(sample_id)["status"], "in_lab")

        self.repo.update_entity = original
        retried = self.service.retry_receipt(self.lab, receipt["id"])
        self.assertEqual(retried["status"], "matched")
        sample = self.service.get(sample_id)
        self.assertEqual(sample["status"], "resulted")
        self.assertEqual(sample["data"]["result"], "positive")

    def test_offline_receipts_merge(self):
        _, sample = self._build_sample("W-1")
        receipt = self.service.create_receipt(
            self.lab,
            {
                "lab_id": "LAB-1",
                "batch_code": "B-1",
                "sample_code": "W-1",
                "result": "positive",
                "result_at": "2026-04-02",
            },
            offline=True,
        )
        self.assertEqual(receipt["status"], "received")
        self.assertFalse(receipt["data"]["synced"])
        # still local: sample untouched
        self.assertEqual(self.service.get(sample["id"])["status"], "in_lab")

        result = self.service.merge_receipts(self.lab)
        self.assertEqual(len(result["merged"]), 1)
        self.assertEqual(result["merged"][0]["status"], "matched")
        receipt = self.service.get(receipt["id"])
        self.assertTrue(receipt["data"]["synced"])
        self.assertEqual(self.service.get(sample["id"])["status"], "resulted")

    def test_offline_receipt_suspended_then_merge(self):
        receipt = self.service.create_receipt(
            self.lab,
            {
                "lab_id": "LAB-1",
                "batch_code": "B-1",
                "sample_code": "UNKNOWN",
                "result": "positive",
                "result_at": "2026-04-02",
            },
            offline=True,
        )
        self.assertEqual(receipt["status"], "received")
        result = self.service.merge_receipts(self.lab)
        self.assertEqual(result["merged"][0]["status"], "suspended")

    def test_lab_role_cannot_create_observation(self):
        from src.domain import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.lab,
                "observation",
                {
                    "event_id": "E-X",
                    "species": "deer",
                    "location": "N",
                    "observed_at": "2026-04-01",
                    "lat": 1.0,
                    "lon": 1.0,
                },
            )


if __name__ == "__main__":
    unittest.main()
