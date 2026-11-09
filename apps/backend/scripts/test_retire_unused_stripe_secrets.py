import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import retire_unused_stripe_secrets as retire


class RetirementTests(unittest.TestCase):
    def test_deployed_lambda_version_reference_is_checked_and_blocks(self):
        cf, ecs, lm = Mock(), Mock(), Mock()
        cf.get_paginator.return_value.paginate.return_value = [{"StackSummaries": []}]
        ecs.get_paginator.return_value.paginate.return_value = [{"clusterArns": []}]
        lm.get_paginator.return_value.paginate.return_value = [{"Functions": [{"FunctionArn": "function:2"}]}]
        name = f"lightbulb/dev/{retire.SUFFIXES[0]}"
        lm.get_function_configuration.return_value = {"Environment": {"Variables": {"SECRET_ID": name}}}
        session = Mock()
        session.client.side_effect = lambda service, **_: {"cloudformation": cf, "ecs": ecs, "lambda": lm}[service]
        with self.assertRaisesRegex(ValueError, "Lambda environment"):
            retire.consumer_checks(session, [name])
        lm.get_paginator.return_value.paginate.assert_called_with(FunctionVersion="ALL")

    def setUp(self):
        self.sm = Mock()
        self.session = Mock()
        self.session.client.side_effect = lambda name, **_: (
            self.sm if name == "secretsmanager" else Mock(get_caller_identity=lambda: {"Account": retire.ACCOUNT})
        )
        self.rows = {
            f"lightbulb/dev/{suffix}": {
                "Name": f"lightbulb/dev/{suffix}",
                "ARN": f"arn:aws:secretsmanager:{retire.REGION}:{retire.ACCOUNT}:secret:lightbulb/dev/{suffix}-abcdef",
                "LastChangedDate": datetime(2026, 8, 1, tzinfo=UTC),
            }
            for suffix in retire.SUFFIXES
        }
        self.sm.describe_secret.side_effect = lambda SecretId: self.rows[SecretId]

    def test_plan_never_reads_values_or_deletes_and_exact_review_hash_required(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(retire, "consumer_checks") as check:
            root = Path(directory)
            plan = retire.run(self.session, "dev", "plan", "", root)
            self.sm.get_secret_value.assert_not_called()
            self.sm.delete_secret.assert_not_called()
            with self.assertRaisesRegex(ValueError, "reviewed"):
                retire.run(self.session, "dev", "execute", "wrong", root)
            self.sm.delete_secret.assert_not_called()
            receipt = retire.run(self.session, "dev", "execute", plan["scopeSha256"], root)
            self.assertEqual(len(receipt["scheduledArns"]), 2)
            self.assertEqual(self.sm.delete_secret.call_count, 2)
            for call in self.sm.delete_secret.call_args_list:
                self.assertEqual(set(call.kwargs), {"SecretId", "RecoveryWindowInDays"})
                self.assertEqual(call.kwargs["RecoveryWindowInDays"], 30)
            self.assertEqual(check.call_count, 3)

    def test_rotation_metadata_or_managed_identity_change_blocks_all_writes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(retire, "consumer_checks"):
            root = Path(directory)
            plan = retire.run(self.session, "dev", "plan", "", root)
            originals = copy.deepcopy(self.rows)
            for field, value in (
                ("LastChangedDate", datetime(2026, 8, 2, tzinfo=UTC)),
                ("OwningService", "service"),
                ("RotationEnabled", True),
                ("DeletedDate", datetime.now(UTC)),
                ("ARN", "arn:other-account"),
            ):
                self.rows = copy.deepcopy(originals)
                self.rows[next(iter(self.rows))][field] = value
                with self.assertRaises(ValueError):
                    retire.run(self.session, "dev", "execute", plan["scopeSha256"], root)
            self.sm.delete_secret.assert_not_called()

    def test_partial_failure_preserves_receipt_and_recovery_window(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(retire, "consumer_checks"):
            root = Path(directory)
            plan = retire.run(self.session, "dev", "plan", "", root)
            self.sm.delete_secret.side_effect = [{}, RuntimeError("provider unavailable")]
            path = root / "receipt.json"
            with self.assertRaises(RuntimeError):
                retire.run(self.session, "dev", "execute", plan["scopeSha256"], root, path)
            receipt = json.loads(path.read_text())
            self.assertEqual(len(receipt["scheduledArns"]), 1)
            self.assertEqual(receipt["scopeSha256"], plan["scopeSha256"])

    def test_any_source_or_deployed_consumer_blocks_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "apps/backend/core/consumer.py"
            path.parent.mkdir(parents=True)
            path.write_text(f"secret = '{retire.SUFFIXES[0]}'")
            with self.assertRaisesRegex(ValueError, "source consumer"):
                retire.run(self.session, "dev", "execute", "anything", root)
            with self.assertRaisesRegex(ValueError, "referenced"):
                retire.assert_unreferenced(
                    {"SecretId": f"lightbulb/dev/{retire.SUFFIXES[0]}"}, [f"lightbulb/dev/{retire.SUFFIXES[0]}"], "task"
                )
            self.sm.delete_secret.assert_not_called()


if __name__ == "__main__":
    unittest.main()
