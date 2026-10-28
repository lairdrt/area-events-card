"""Offline syntax and regression guard for the PowerShell-embedded Python payloads.

AST parsing never executes these scripts or contacts either production host.
"""
import ast
import hashlib
import pathlib
import re
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
DEPLOYER = REPO / "scripts" / "deploy-area-events.ps1"
BLOCKS = re.compile(
    r"(?m)^[ \t]*\$(?:py|cleanup|imagePy|installPy|script:ImageDepsPy)=(?:\$script:ImageDepsPy\+\"`n\"\+)?@'\r?\n([\s\S]*?)\r?\n'@"
)
# A here-string drops its final newline, so PowerShell glues the helper's last line to the body's first unless a newline is added explicitly.
HELPER = re.compile(r"(?m)^[ \t]*\$script:ImageDepsPy=@'\r?\n([\s\S]*?)\r?\n'@")
COMPOSED = re.compile(
    r"(?m)^[ \t]*\$(\w+)=\$script:ImageDepsPy\+\"`n\"\+@'\r?\n([\s\S]*?)\r?\n'@"
)

class EmbeddedDeploymentPythonTests(unittest.TestCase):
    def test_all_embedded_python_blocks_parse(self):
        text = DEPLOYER.read_text(encoding="utf-8")
        blocks = BLOCKS.findall(text)
        self.assertEqual(12, len(blocks), "all remote Python blocks must be covered")
        for n, block in enumerate(blocks, 1):
            with self.subTest(block=n):
                ast.parse(block, filename=f"embedded_{n}.py")

    def test_helper_composed_scripts_parse_as_powershell_assembles_them(self):
        text = DEPLOYER.read_text(encoding="utf-8")
        helper = HELPER.search(text).group(1)  # a here-string value has no trailing newline
        composed = COMPOSED.findall(text)
        self.assertEqual(6, len(composed), "installed-source, rehearsal, image staging, deployment, verification and recovery")
        self.assertEqual(6, text.count("$script:ImageDepsPy+"), "every use of the helper must add the newline")
        for variable, body in composed:
            with self.subTest(variable=variable, first_line=body.splitlines()[0]):
                ast.parse(helper + "\n" + body, filename=f"assembled_{variable}.py")
        # The defect itself: without the newline the same pieces are not valid Python.
        with self.assertRaises(SyntaxError):
            ast.parse(helper + composed[0][1])

    def test_remote_time_budgets_fit_the_hardening_limit_and_their_nested_timeouts(self):
        text = DEPLOYER.read_text(encoding="utf-8")
        hardening = (REPO / "scripts" / "area-events-deploy-hardening.psm1").read_text(encoding="utf-8")
        lo, hi = map(int, re.search(r"ValidateRange\((\d+),(\d+)\)\]\[int\]\$TimeoutSeconds", hardening).groups())
        calls = re.findall(r"(?m)Invoke-CubiPython\s+(\S+)[^\n]*?\s(\d+)\s*(?:\||$)", text)
        self.assertGreaterEqual(len(calls), 11, "every Invoke-CubiPython call must state its timeout")
        for code, seconds in calls:
            with self.subTest(code=code):
                self.assertTrue(lo <= int(seconds) <= hi, f"{code}: {seconds}s is outside the permitted {lo}-{hi}s")
        self.assertEqual(len(re.findall(r"Invoke-CubiPython\s+[$\"]", text)), len(calls), "a call has no explicit timeout")
        helper = HELPER.search(text).group(1)
        install_default = int(re.search(r"def ae_install_images\(.*?timeout=(\d+)\)", helper).group(1))
        smoke = int(re.search(r"def ae_image_smoke.*?\n.*?timeout=(\d+),", helper).group(1))
        timeouts = {code: int(seconds) for code, seconds in calls}
        for variable, body in COMPOSED.findall(text):
            total = 0
            for line in body.splitlines():
                total += sum(int(n) for n in re.findall(r"timeout=(\d+)", line))
                if "ae_install_images(" in line and "timeout=" not in line:
                    total += install_default
                if "ae_image_smoke(" in line:
                    total += smoke
            with self.subTest(variable=variable, nested=total):
                self.assertIn("$" + variable, timeouts, "composed block is never run")
                budget = timeouts["$" + variable] - 10  # the remote command gets 10 s less than the call
                self.assertLessEqual(total + 60, budget, f"nested timeouts total {total}s (+60s slack) against a {budget}s remote budget")

    def test_blob_comparison_accepts_only_line_ending_differences(self):
        # Execute only the extracted pure hash helper; never the remote deployment.
        source = DEPLOYER.read_text(encoding="utf-8")
        block = next(b for b in BLOCKS.findall(source) if "def blob_sha(path):" in b)
        tree = ast.parse(block)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "blob_sha"
        )
        namespace = {"hashlib": hashlib}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "<blob_sha>", "exec"), namespace)
        blob_sha = namespace["blob_sha"]
        data = b"[project]\nname = 'area-events'\nversion = '0.2.0'\n"
        expected = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            for filename in ("pyproject.toml", "service.py"):
                path = pathlib.Path(tmp, filename)
                with self.subTest(file=filename):
                    path.write_bytes(data)
                    self.assertEqual(expected, blob_sha(str(path)))
                    path.write_bytes(data.replace(b"\n", b"\r\n"))
                    self.assertEqual(expected, blob_sha(str(path)))
                    path.write_bytes(data.replace(b"0.2.0", b"0.2.1").replace(b"\n", b"\r\n"))
                    self.assertNotEqual(expected, blob_sha(str(path)))
                    path.write_bytes(data.replace(b"\n", b"\r\n") + b" ")
                    self.assertNotEqual(expected, blob_sha(str(path)))

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
