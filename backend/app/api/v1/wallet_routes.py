from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.services.wallet_auth import WalletAuthService

router = APIRouter(prefix="/wallet", tags=["wallet"])
wallet_auth = WalletAuthService()


class NonceIn(BaseModel):
    address: str = Field(min_length=42, max_length=42)


class VerifyIn(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    signature: str


@router.post("/nonce")
async def wallet_nonce(payload: NonceIn) -> dict:
    if not payload.address.startswith("0x"):
        raise HTTPException(status_code=400, detail="invalid wallet address")
    return {"data": wallet_auth.issue_nonce(payload.address)}


@router.post("/verify")
async def wallet_verify(payload: VerifyIn) -> dict:
    try:
        session = wallet_auth.verify_signature(payload.address, payload.signature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "data": {
            "token": session.token,
            "address": session.address,
            "expires_in_seconds": wallet_auth.session_ttl_seconds,
        }
    }


@router.get("/session")
async def wallet_session(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    session = wallet_auth.get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return {"data": {"address": session.address, "connected": True}}


@router.post("/disconnect")
async def wallet_disconnect(authorization: str | None = Header(default=None)) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        wallet_auth.revoke(token)
    return {"data": {"disconnected": True}}
