import hashlib
import json
import plistlib
from pathlib import Path

from fastapi.testclient import TestClient

from wortling.api import create_app


def test_shortcut_control_flow_groups_are_distinct_and_balanced():
    root = Path(__file__).resolve().parents[1]
    workflow = plistlib.loads((root / "shortcuts/CaptureGerman.plist").read_bytes())
    seen = set()
    stack = []
    output_ids = []
    for action in workflow["WFWorkflowActions"]:
        params = action.get("WFWorkflowActionParameters", {})
        if "UUID" in params:
            output_ids.append(params["UUID"])
        if "GroupingIdentifier" not in params:
            continue
        group = params["GroupingIdentifier"]
        mode = params["WFControlFlowMode"]
        if mode == 0:
            assert group not in seen, "Separate control-flow blocks share an identifier"
            seen.add(group)
            stack.append((action["WFWorkflowActionIdentifier"], group))
        else:
            assert stack[-1] == (action["WFWorkflowActionIdentifier"], group)
            if mode == 2:
                stack.pop()
    assert not stack
    assert len(output_ids) == len(set(output_ids))


def test_shortcut_extracts_native_images_and_recovers_camera_filenames():
    root = Path(__file__).resolve().parents[1]
    workflow = plistlib.loads((root / "shortcuts/CaptureGerman.plist").read_bytes())
    actions = workflow["WFWorkflowActions"]
    extraction = next(
        a for a in actions if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.detect.images"
    )
    params = extraction["WFWorkflowActionParameters"]
    assert params["WFInput"]["Value"]["VariableName"] == "input"
    assert params["WFInput"]["WFSerializationType"] == "WFTextTokenAttachment"
    picker = next(
        a for a in actions if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.selectphoto"
    )
    assert not picker["WFWorkflowActionParameters"].get("WFSelectMultiplePhotos", False)
    assert "iOS shared only a photo filename" in str(workflow)
    assert "shortcut-v3:" in str(workflow)


def test_shortcut_placeholder_and_source_match_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "shortcuts/manifest.json").read_text())
    paths = {
        "source_sha256": root / "shortcuts/CaptureGerman.cherri",
        "unsigned_sha256": root / "shortcuts/CaptureGerman.plist",
    }
    for key, path in paths.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[key]
    workflow = plistlib.loads(paths["unsigned_sha256"].read_bytes())
    question = workflow["WFWorkflowImportQuestions"][0]
    assert question["DefaultValue"] == "YOUR_WORTLING_KEY"
    assert (
        workflow["WFWorkflowActions"][question["ActionIndex"]]["WFWorkflowActionParameters"][
            question["ParameterKey"]
        ]
        == ""
    )
    assert "ActionExtension" in workflow["WFWorkflowTypes"]
    actions = workflow["WFWorkflowActions"]
    requests = [
        a["WFWorkflowActionParameters"]
        for a in actions
        if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
    ]
    assert len(requests) == 5
    for request in requests:
        assert request["WFHTTPMethod"] == "POST"
        assert "wortling.example.invalid/v1/captures" in str(request["WFURL"])
        assert request["WFHTTPBodyType"] == "JSON"
        fields = request["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
        keys = {field["WFKey"]["Value"]["string"] for field in fields}
        assert keys in [
            {"client_ts", "text"},
            {"client_ts", "url"},
            {"client_ts", "text", "url"},
            {"client_ts", "image_b64", "image_mime"},
        ]
    assert any(
        a["WFWorkflowActionIdentifier"] == "is.workflow.actions.image.convert" for a in actions
    )
    assert not any(
        a["WFWorkflowActionIdentifier"] == "is.workflow.actions.openurl" for a in actions
    )


def test_shortcut_download_and_personal_key_require_authenticated_explicit_copy(
    settings, tmp_path, monkeypatch
):
    from test_browser_auth import BASE, ORIGIN, setup

    # Exercise download/auth without publishing a personal signed binary.
    asset = tmp_path / "assets/CaptureGerman.shortcut"
    asset.parent.mkdir()
    asset.write_bytes(b"AEA1synthetic-test-template")
    monkeypatch.setattr("wortling.shortcut.files", lambda package: tmp_path)
    _, link_token = setup(settings)
    with TestClient(create_app(settings), base_url=BASE) as client:
        download = client.get("/v1/shortcut")
        assert download.status_code == 200
        assert download.content.startswith(b"AEA1")
        assert "application/x-apple-shortcut" in download.headers["content-type"]
        assert 'filename="Capture German v3.shortcut"' in download.headers["content-disposition"]
        assert "a" * 48 not in download.content.decode("latin1")
        assert client.post("/v1/shortcut/key", headers=ORIGIN).status_code == 401
        client.post("/v1/session", json={"token": link_token}, headers=ORIGIN).raise_for_status()
        assert client.post("/v1/shortcut/key").status_code == 403
        key = client.post("/v1/shortcut/key", headers=ORIGIN)
        assert key.json() == {"token": "a" * 48}
        assert key.headers["cache-control"] == "no-store"


def test_shortcut_download_without_local_build_is_explicit(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("wortling.shortcut.files", lambda package: tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/shortcut")
        assert response.status_code == 503
        assert "build a Shortcut" in response.json()["detail"]
