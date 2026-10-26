"""Offline syntax and regression guard for the PowerShell-embedded Python payloads.

AST parsing never executes these scripts or contacts either production host.
"""
import ast
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
DEPLOYER = REPO / "scripts" / "deploy-area-events.ps1"
BLOCKS = re.compile(
    r"(?m)^[ \t]*\$(?:py|cleanup)=@'\r?\n([\s\S]*?)\r?\n'@"
)

class EmbeddedDeploymentPythonTests(unittest.TestCase):
    def test_all_embedded_python_blocks_parse(self):
        text = DEPLOYER.read_text(encoding="utf-8")
        blocks = BLOCKS.findall(text)
        self.assertEqual(9, len(blocks), "all remote Python blocks must be covered")
        for n, block in enumerate(blocks, 1):
            with self.subTest(block=n):
                ast.parse(block, filename=f"embedded_{n}.py")

    def test_runtime_reconciliation_checks_wheel_and_service(self):
        text = DEPLOYER.read_text(encoding="utf-8")
        section = text.split("function Test-CubiInstalledSource {", 1)[1].split(
            "function Invoke-DeploymentPlan(", 1
        )[0]
        for required in (
            "git.exe",
            "'ls-tree'",
            "blob_sha(path)",
            "site-packages",
            "area-events-discovery.service",
            "'-m','area_events'",
            "AE_EXPECTED",
        ):
            with self.subTest(required=required):
                self.assertIn(required, section)

    def test_planner_does_not_trust_joint_receipt(self):
        text = DEPLOYER.read_text(encoding="utf-8")
        planner = text.split("function Invoke-DeploymentPlan(", 1)[1].split(
            "function Invoke-Backups(", 1
        )[0]
        self.assertNotIn("$remote.receipt", planner)
        self.assertIn("Resolve-AreaEventsComponentPlan", planner)
        self.assertIn("Test-HaAssets", planner)

if __name__ == "__main__":
    unittest.main()
