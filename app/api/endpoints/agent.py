import os
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from app.api.dependencies.tenancy import bind_tenant_context
from app.core.security.auth import CurrentUser
from app.core.config import get_settings
from jose import jwt
import uuid
import datetime

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

@router.post("/token", summary="Generate a long-lived agent token")
async def generate_agent_token(
    current: CurrentUser = Depends(bind_tenant_context)
) -> dict[str, str]:
    """Generate a JWT token that lasts for 10 years, specifically for use by the gateway agent."""
    settings = get_settings()
    now = datetime.datetime.now(datetime.timezone.utc)
    ttl = datetime.timedelta(days=3650)  # 10 years
    body = {
        "sub": str(current.user_id),
        "org": str(current.org_id),
        "email": current.email,
        "role": current.role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(body, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return {"token": token}
