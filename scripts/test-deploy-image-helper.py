"""Behavior check for the Python helper embedded in deploy-area-events.ps1 (`$script:ImageDepsPy`).

Usage: test-deploy-image-helper.py <helper.py extracted from the script> <scratch directory>
Nothing is installed and no network is used: pip and the self-check subprocess are replaced.
"""
import importlib.util
import os
import sys
import types

helper_path, scratch = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("helper", helper_path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

bare, extra = os.path.join(scratch, "bare"), os.path.join(scratch, "extra")
for folder in (bare, extra):
    os.mkdir(folder)
with open(os.path.join(bare, "pyproject.toml"), "w") as handle:
    handle.write("[project]\nname='x'\nversion='1'\n")
with open(os.path.join(extra, "pyproject.toml"), "w") as handle:
    handle.write("[project]\nname='x'\nversion='1'\ndependencies=['rapidfuzz==3.14.6']\n[project.optional-dependencies]\nimages=['goose3==3.1.22','lxml==6.1.3']\n")

failed = types.SimpleNamespace(returncode=1, stdout='{"ok": false}', stderr="boom")
passed = types.SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")
calls = []


def run(result):
    def fake(command, **options):
        calls.append(command)
        return result
    helper._sp.run = fake


assert helper.ae_install_images("python", bare, required=False) is False, "an absent extra is optional for old code"
try:
    helper.ae_install_images("python", bare)
    raise SystemExit("a missing images extra was accepted")
except RuntimeError:
    pass
run(failed)
try:
    helper.ae_install_images("python", extra)
    raise SystemExit("a failed installation was accepted")
except RuntimeError:
    pass
assert "--only-binary=:all:" in calls[0] and "goose3==3.1.22" in calls[0] and "lxml==6.1.3" in calls[0] and "rapidfuzz==3.14.6" in calls[0], calls
try:
    helper.ae_image_smoke("python", extra)
    raise SystemExit("a failed self-check was accepted")
except RuntimeError:
    pass
run(passed)
assert helper.ae_install_images("python", extra) is True
assert helper.ae_image_smoke("python", extra) == '{"ok": true}'
assert calls[-1][:3] == ["python", "-I", "-m"] and calls[-1][3] == "area_events.image_extractor.selfcheck", calls[-1]
print("helper ok")
