from __future__ import annotations

import os
from importlib.resources import files
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from wortling.browser_auth import require_browser_origin
from wortling.capture import learner_auth
from wortling.config import Learner

router = APIRouter(prefix="/v1/shortcut")


@router.get("")
def download_shortcut():
    asset = files("wortling").joinpath("assets/CaptureGerman.shortcut")
    if not asset.is_file():
        raise HTTPException(503, "The operator must build a Shortcut for this deployment first")
    return Response(
        asset.read_bytes(),
        media_type="application/x-apple-shortcut",
        headers={"Content-Disposition": 'attachment; filename="Capture German v3.shortcut"'},
    )


@router.post("/key")
def shortcut_key(
    request: Request, response: Response, user: Annotated[Learner, Depends(learner_auth)]
):
    require_browser_origin(request)
    token = os.environ.get(user.capture_token_env)
    if not token:
        raise HTTPException(503, "A Shortcut key has not been configured for this learner")
    response.headers["Cache-Control"] = "no-store"
    return {"token": token}
