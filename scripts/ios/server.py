"""Isolated real capture API and signed test workflow; never loads live config."""

import os
import plistlib
import subprocess
from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw

from wortling.api import create_app
from wortling.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/ios-shortcut-test"
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
TOKEN = "wortling-isolated-ios-test-" + "x" * 40
os.environ["WORTLING_IOS_TEST_TOKEN"] = TOKEN
template = Path(
    os.environ.get("WORTLING_SIMULATOR_TEMPLATE", ROOT / "shortcuts/CaptureGerman.plist")
)
workflow = plistlib.loads(template.read_bytes())
for action in workflow["WFWorkflowActions"]:
    params = action.get("WFWorkflowActionParameters", {})
    if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl":
        assert params["WFURL"] == "https://wortling.example.invalid/v1/captures"
        params["WFURL"] = "http://127.0.0.1:8791/v1/captures"
unsigned = DATA / "unsigned.shortcut"
unsigned.write_bytes(plistlib.dumps(workflow))
signed = DATA / "Capture German Test.shortcut"
subprocess.run(
    ["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)],
    check=True,
)
fixture = Image.new("RGB", (4000, 3000), "white")
ImageDraw.Draw(fixture).text((200, 200), "Bitte Material sammeln.", fill="black", font_size=150)
fixture.save(DATA / "IMG_fixture.jpg", quality=95)
settings = Settings.model_validate(
    {
        "data_dir": DATA / "api",
        "users": [
            {
                "id": "simulator",
                "display_name": "Simulator",
                "capture_token_env": "WORTLING_IOS_TEST_TOKEN",
                "ankiweb_user_env": "UNUSED_IOS_TEST_USER",
                "ankiweb_pass_env": "UNUSED_IOS_TEST_PASS",
                "processing_enabled": False,
                "writes_enabled": False,
            }
        ],
    }
)
app = create_app(settings)


@app.get("/test/info")
def info():
    return {"service": "wortling-isolated-ios-test", "template": str(template)}


@app.get("/test/shortcut")
def shortcut():
    return FileResponse(signed, media_type="application/x-apple-shortcut", filename=signed.name)


@app.get("/test/captures")
def results():
    with app.state.db.connect() as db:
        captures = []
        for row in db.execute("SELECT id,text,image_path,idempotency_key FROM captures"):
            result = dict(row)
            if row["image_path"]:
                with Image.open(settings.data_dir / row["image_path"]) as image:
                    result.update(format=image.format, width=image.width, height=image.height)
            captures.append(result)
        return captures


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8791)
