"""기존 실행 명령(uvicorn main:app)을 유지하는 진입점."""
from app.main import app

__all__ = ["app"]
