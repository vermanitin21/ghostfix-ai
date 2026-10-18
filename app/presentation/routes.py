"""Read-only judge UI routes. Never runs repairs, models or Docker; renders recorded evidence only."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import get_settings
from app.presentation.bundle import load_bundle, local_canonical_status
from app.presentation.view import build_view

HERE = Path(__file__).resolve().parent
BUNDLE_PATH = HERE / "data" / "canonical-self-correction.json"
STATIC_DIR = HERE / "static"
CANONICAL_DIR = HERE.parents[1] / "artifacts" / "hackathon-demo"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

router = APIRouter(tags=["presentation"])


def _secrets() -> tuple[str, ...]:
    try:
        key = get_settings().nebius_api_key
        return (key.get_secret_value(),) if key and key.get_secret_value() else ()
    except Exception:
        return ()


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers=SECURITY_HEADERS)


@router.get("/api/replay")
async def replay() -> JSONResponse:
    """Recorded-evidence view model only; never a live repair."""
    try:
        bundle = load_bundle(BUNDLE_PATH, _secrets())
        view = build_view(bundle, local_canonical_status(bundle, CANONICAL_DIR))
    except Exception:
        # Generic by design: no paths, exception text or partial evidence reach the browser.
        return JSONResponse({"state": "evidence_unavailable",
                             "reason": "Recorded evidence is missing or failed validation."},
                            status_code=503, headers=SECURITY_HEADERS)
    return JSONResponse(view.model_dump(mode="json"), headers=SECURITY_HEADERS)
