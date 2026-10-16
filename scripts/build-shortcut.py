"""Build a credential-free template with Cherri, then sign it using Apple's CLI."""

import argparse
import hashlib
import json
import plistlib
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--compiler", default="cherri")
parser.add_argument("--base-url", required=True, help="HTTPS origin of your deployment")
args = parser.parse_args()
origin = urlsplit(args.base_url)
if (
    origin.scheme != "https"
    or not origin.hostname
    or origin.username
    or origin.password
    or origin.path not in ("", "/")
    or origin.query
    or origin.fragment
):
    parser.error("--base-url must be an HTTPS origin without credentials, path, or query")
base_url = args.base_url.rstrip("/")
root = Path(__file__).resolve().parents[1]
source = root / "shortcuts/CaptureGerman.cherri"
signed = root / "src/wortling/assets/CaptureGerman.shortcut"
with tempfile.TemporaryDirectory(prefix="wortling-shortcut-") as folder:
    temporary = Path(folder)
    (temporary / source.name).write_text(
        source.read_text().replace("https://wortling.example.invalid", base_url)
    )
    subprocess.run(
        [args.compiler, str(temporary / source.name), "--skip-sign", "--no-ansi"],
        check=True,
    )
    unsigned = temporary / "Capture German_unsigned.shortcut"
    workflow = plistlib.loads(unsigned.read_bytes())
    # The pinned compiler's --derive-uuids mode gives anonymous control-flow
    # blocks the same UUID. Signing accepts that malformed structure.
    groups = set()
    stack = []
    for action in workflow["WFWorkflowActions"]:
        parameters = action.get("WFWorkflowActionParameters", {})
        if "GroupingIdentifier" not in parameters:
            continue
        group = parameters["GroupingIdentifier"]
        if parameters["WFControlFlowMode"] == 0:
            assert group not in groups, "Duplicate control-flow group"
            groups.add(group)
            stack.append((action["WFWorkflowActionIdentifier"], group))
        else:
            assert stack[-1] == (action["WFWorkflowActionIdentifier"], group)
            if parameters["WFControlFlowMode"] == 2:
                stack.pop()
    assert not stack, "Unclosed control-flow group"
    for action in workflow["WFWorkflowActions"]:
        if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.base64encode":
            parameters = action["WFWorkflowActionParameters"]
            parameters.pop("input", None)
            parameters["WFEncodeMode"] = "Encode"
    question = workflow["WFWorkflowImportQuestions"][0]
    assert question["DefaultValue"] == "YOUR_WORTLING_KEY"
    assert (
        workflow["WFWorkflowActions"][question["ActionIndex"]]["WFWorkflowActionParameters"][
            question["ParameterKey"]
        ]
        == ""
    )
    # No learner credential is read or inserted. Apple receives only this template.
    raw = plistlib.dumps(workflow, fmt=plistlib.FMT_XML)
    unsigned.write_bytes(raw)
    signed.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "shortcuts",
            "sign",
            "--mode",
            "anyone",
            "--input",
            str(unsigned),
            "--output",
            str(signed),
        ],
        check=True,
    )
    # Apple's signer creates mode 0600; this public template must be readable
    # by the container's unprivileged runtime user after COPY.
    signed.chmod(0o644)
    build_dir = root / "data/shortcut-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "CaptureGerman.plist").write_bytes(raw)
manifest = {
    "compiler": "https://github.com/electrikmilk/cherri",
    "compiler_commit": "606c9a25e481f08bff43bedb93be5e3d9b1229c9",
    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    "unsigned_sha256": hashlib.sha256(raw).hexdigest(),
    "signed_sha256": hashlib.sha256(signed.read_bytes()).hexdigest(),
}
(build_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("Built and signed credential-free Capture German template")
