"""GhostFix AI API entry point."""

from fastapi import FastAPI
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


app = FastAPI(title="GhostFix AI", version="0.1.0")


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Report whether the API process is available."""
    return HealthResponse(status="ok")
