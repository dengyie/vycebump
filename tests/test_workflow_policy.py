#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 工作流静态安全策略与供应链回归检查"""

import os
import re
import unittest

WORKFLOW_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "renew.yml")
)


class TestWorkflowPolicy(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(WORKFLOW_PATH), f"Workflow file not found at {WORKFLOW_PATH}")
        with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_minimal_permissions_declared(self):
        self.assertIn("permissions:", self.content)
        self.assertIn("contents: read", self.content)

    def test_concurrency_group_declared(self):
        self.assertIn("concurrency:", self.content)
        self.assertIn("cancel-in-progress: false", self.content)

    def test_renewal_schedule_guarantees_fallback_within_three_hours(self):
        """2026-11-02 事故回归：CAPTCHA 故障打穿窗口致停机后，兜底唤醒等待不得再依赖 6h 间隔。

        主巡检 (UTC 00/06/12/18 :17) + 补充巡检 (UTC 03/09/15/21 :17) 必须交替等距覆盖，
        使机器停机后 /start 兜底的最大等待 ≤ 3h + GitHub 调度抖动。
        """
        crons = re.findall(r"- cron:\s*'([^']+)'", self.content)
        self.assertIn("17 */6 * * *", crons, "缺少每 6 小时主巡检 cron")
        self.assertIn("17 3,9,15,21 * * *", crons, "缺少每 3 小时补充巡检 cron（UTC 03/09/15/21 :17）")

        # 主巡检与补充巡检时刻交替，验证全 24 小时无 >3h 的调度空窗
        hours = set()
        for expr in crons:
            hour_field = expr.split()[1]
            if hour_field == "*/6":
                hours.update(range(0, 24, 6))
            else:
                hours.update(int(h) for h in hour_field.split(","))
        self.assertEqual(sorted(hours), [0, 3, 6, 9, 12, 15, 18, 21])

    def test_actions_pinned_to_immutable_commit_hashes(self):
        # Disallow mutable tags like @v4 or @v5 without SHA
        unpinned_patterns = [
            r"uses:\s+actions/checkout@v\d",
            r"uses:\s+actions/setup-python@v\d",
            r"uses:\s+actions/upload-artifact@v\d",
        ]
        for pattern in unpinned_patterns:
            self.assertIsNone(
                re.search(pattern, self.content),
                f"Found unpinned action matching {pattern}"
            )

        # Must have full 40-character hex commit SHA
        sha_pattern = r"uses:\s+actions/[\w-]+@[0-9a-fA-F]{40}"
        matches = re.findall(sha_pattern, self.content)
        self.assertGreaterEqual(len(matches), 3, "Expected at least 3 actions pinned to 40-char SHA")

    def test_singbox_integrity_and_fail_closed(self):
        self.assertIn("SINGBOX_SHA256=", self.content)
        self.assertIn("sha256sum --check", self.content)
        self.assertIn("Fail-Closed", self.content)

    def test_no_sensitive_renew_state_in_artifacts(self):
        self.assertNotIn("renew_state.json", self.content)

    def test_captcha_prerequisites_are_verified_before_renewal(self):
        # 续期需要 Chrome + Node 求解 Bestcap CAPTCHA，缺失时应尽早失败
        self.assertIn("CAPTCHA_SOLVER", self.content)
        self.assertIn("CHROME_BIN", self.content)
        check_index = self.content.find("Verify CAPTCHA Solver Prerequisites")
        run_index = self.content.find("Run Vyce Renew Script")
        self.assertGreater(check_index, 0, "缺少 CAPTCHA 前置检查步骤")
        self.assertGreater(run_index, check_index, "前置检查必须排在续期脚本之前")
        self.assertIn("command -v node", self.content)
        self.assertIn("command -v google-chrome", self.content)


if __name__ == "__main__":
    unittest.main()
