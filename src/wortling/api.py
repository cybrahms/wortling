from __future__ import annotations

import os
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from fastapi.responses import FileResponse, HTMLResponse

from wortling.browser_auth import router as browser_router
from wortling.capture import BodyLimit, learner_auth, router
from wortling.config import Learner, Settings, load_settings
from wortling.db import Database
from wortling.report import learner_report
from wortling.shortcut import router as shortcut_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.initialize(settings)
        yield

    app = FastAPI(title="Wortling", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    app.include_router(router)
    app.include_router(browser_router)
    app.include_router(shortcut_router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def trial_page():
        return HTMLResponse(
            files("wortling").joinpath("trial.html").read_text(),
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page():
        return HTMLResponse(
            files("wortling").joinpath("setup.html").read_text(),
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/v1/report")
    def report(user: Annotated[Learner, Depends(learner_auth)]):
        return learner_report(db, user)

    @app.get("/v1/captures/{capture_id}/image")
    def capture_image(capture_id: str, user: Annotated[Learner, Depends(learner_auth)]):
        from fastapi import HTTPException

        with db.connect() as conn:
            row = conn.execute(
                "SELECT image_path,image_mime FROM captures WHERE id=? AND user_id=?",
                (capture_id, user.id),
            ).fetchone()
        if row is None or not row["image_path"]:
            raise HTTPException(404, "Image not found")
        return FileResponse(db.data_dir / row["image_path"], media_type=row["image_mime"])

    @app.get("/v1/health")
    def health(response: Response):
        try:
            with db.connect() as conn:
                conn.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
        except Exception:
            response.status_code = 503
            return {"status": "unavailable"}
        return {
            "status": "ok",
            "version": "0.1.0",
            "build": os.environ.get("WORTLING_BUILD_SHA", "development"),
        }

    return app
