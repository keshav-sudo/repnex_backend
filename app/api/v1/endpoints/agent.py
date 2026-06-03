from __future__ import annotations

import os
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(prefix="/agent", tags=["agent"])

AGENT_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "repnex-agent.py")

@router.get("/download", summary="Download the repnex-agent.py script")
async def download_agent():
    """Serve the repnex-agent.py script for download."""
    path = os.path.abspath(AGENT_FILE)
    return FileResponse(
        path=path,
        media_type="text/x-python",
        filename="repnex-agent.py",
        headers={"Content-Disposition": "attachment; filename=repnex-agent.py"},
    )
