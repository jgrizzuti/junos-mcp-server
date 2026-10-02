#!/usr/bin/env python3
"""Unit tests for commit check, dry run and commit confirmed.

Covers load_and_commit_config (commit check before every commit, dry_run,
confirm_timeout_mins), render_and_apply_j2_template (confirm_timeout_mins)
and the confirm_commit tool.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import jmcp
from jnpr.junos.exception import CommitError
from lxml import etree
from tests.test_device_data import get_device

CONFIG = "set system host-name foo"
DIFF = "+ set system host-name foo"


def _make_context():
    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    return ctx


def _load_args(**overrides):
    args = {"router_name": "router1", "config_text": CONFIG, "config_format": "set"}
    args.update(overrides)
    return args


class _DeviceTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_devices = jmcp.devices.copy()
        jmcp.devices = {"router1": get_device("router1")}
        jmcp.connection_pool.close_all(shutdown=False)

        device_patch = patch("jmcp.Device")
        config_patch = patch("jmcp.Config")
        mock_device_cls = device_patch.start()
        self.mock_config_cls = config_patch.start()
        self.addCleanup(device_patch.stop)
        self.addCleanup(config_patch.stop)

        self.device = MagicMock()
        self.device.connected = True
        mock_device_cls.return_value = self.device

        # Config(...) is used directly (load_and_commit_config, confirm_commit)
        # and as a context manager (render_and_apply_j2_template); both yield cu.
        self.cu = MagicMock()
        self.cu.diff.return_value = DIFF
        self.cu.commit_check.return_value = True
        self.cu.__enter__.return_value = self.cu
        self.cu.__exit__.return_value = False
        self.mock_config_cls.return_value = self.cu

    def tearDown(self):
        jmcp.devices = self._orig_devices
        jmcp.connection_pool.close_all(shutdown=False)


class ParseConfirmTimeoutTests(unittest.TestCase):
    def test_none_means_plain_commit(self):
        self.assertIsNone(jmcp._parse_confirm_timeout(None))

    def test_valid_range(self):
        self.assertEqual(jmcp._parse_confirm_timeout(1), 1)
        self.assertEqual(jmcp._parse_confirm_timeout(65535), 65535)

    def test_rejects_out_of_range_and_wrong_types(self):
        for bad in (0, -5, 65536, True, "10", 2.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                jmcp._parse_confirm_timeout(bad)


class LoadAndCommitTests(_DeviceTestCase):
    def _run(self, **overrides):
        return asyncio.run(
            jmcp.handle_load_and_commit_config(_load_args(**overrides), _make_context())
        )[0].text

    def test_commit_check_runs_before_plain_commit(self):
        text = self._run()
        self.assertIn("successfully loaded and committed", text)
        self.cu.commit_check.assert_called_once()
        self.cu.commit.assert_called_once()
        self.assertNotIn("confirm", self.cu.commit.call_args.kwargs)
        self.assertNotIn("Commit confirmed", text)

    def test_failed_commit_check_does_not_commit(self):
        rsp = etree.XML(
            "<rpc-reply><rpc-error><error-severity>error</error-severity>"
            "<error-message>missing mandatory statement</error-message>"
            "</rpc-error></rpc-reply>"
        )
        self.cu.commit_check.side_effect = CommitError(rsp=rsp)
        text = self._run()
        self.assertTrue(text.startswith("Failed commit check on router1"))
        self.assertIn("missing mandatory statement", text)
        self.cu.commit.assert_not_called()
        self.cu.rollback.assert_called_once()
        self.cu.unlock.assert_called_once()

    def test_commit_check_false_does_not_commit(self):
        self.cu.commit_check.return_value = False
        text = self._run()
        self.assertTrue(text.startswith("Failed commit check on router1"))
        self.cu.commit.assert_not_called()

    def test_dry_run_checks_and_rolls_back(self):
        text = self._run(dry_run=True)
        self.assertIn("Dry run: commit check passed on router1", text)
        self.assertIn(DIFF, text)
        self.cu.commit_check.assert_called_once()
        self.cu.commit.assert_not_called()
        self.cu.rollback.assert_called_once()
        self.cu.unlock.assert_called_once()

    def test_confirm_timeout_uses_commit_confirmed(self):
        text = self._run(confirm_timeout_mins=5)
        self.assertEqual(self.cu.commit.call_args.kwargs["confirm"], 5)
        self.assertIn("roll back in 5 minute(s)", text)
        self.assertIn("confirm_commit", text)

    def test_invalid_confirm_timeout_never_touches_device(self):
        text = self._run(confirm_timeout_mins=0)
        self.assertTrue(text.startswith("Error: confirm_timeout_mins"))
        self.mock_config_cls.assert_not_called()

    def test_error_results_are_flagged(self):
        self.cu.commit_check.return_value = False
        result = asyncio.run(
            jmcp.handle_load_and_commit_config(_load_args(), _make_context())
        )
        self.assertTrue(jmcp._is_error_content(result))


class RenderJ2ConfirmTests(_DeviceTestCase):
    def _run(self, **overrides):
        args = {
            "template_content": "set system host-name {{ name }}",
            "vars_content": "name: foo",
            "router_name": "router1",
            "apply_config": True,
        }
        args.update(overrides)
        return asyncio.run(
            jmcp.handle_render_and_apply_j2_template(args, _make_context())
        )[0].text

    def test_confirm_timeout_passed_to_commit(self):
        text = self._run(confirm_timeout_mins=10)
        self.assertEqual(self.cu.commit.call_args.kwargs["confirm"], 10)
        self.assertIn("roll back in 10 minute(s)", text)

    def test_plain_commit_without_confirm(self):
        self._run()
        self.assertNotIn("confirm", self.cu.commit.call_args.kwargs)

    def test_invalid_confirm_timeout_rejected(self):
        text = self._run(confirm_timeout_mins=70000)
        self.assertTrue(text.startswith("❌ Error: confirm_timeout_mins"))
        self.mock_config_cls.assert_not_called()


class ConfirmCommitTests(_DeviceTestCase):
    def _run(self, router_name="router1"):
        return asyncio.run(
            jmcp.handle_confirm_commit({"router_name": router_name}, _make_context())
        )[0].text

    def test_confirms_with_clean_candidate(self):
        self.cu.diff.return_value = None
        text = self._run()
        self.assertIn("Commit confirmed on router1", text)
        self.cu.commit.assert_called_once()
        self.assertNotIn("confirm", self.cu.commit.call_args.kwargs)
        self.cu.unlock.assert_called_once()

    def test_refuses_when_candidate_is_dirty(self):
        text = self._run()
        self.assertTrue(text.startswith("Failed to confirm commit on router1"))
        self.assertIn(DIFF, text)
        self.cu.commit.assert_not_called()
        self.cu.unlock.assert_called_once()

    def test_unknown_router(self):
        text = self._run("nope")
        self.assertIn("not found", text)
        self.mock_config_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
