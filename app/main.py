"""GhostFix AI API entry point."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.presentation.routes import STATIC_DIR, router as presentation_router


class HealthResponse(BaseModel):
    status: str


app = FastAPI(title="GhostFix AI", version="0.1.0")
app.include_router(presentation_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Report whether the API process is available."""
    return HealthResponse(status="ok")
