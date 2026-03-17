from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass
class WalletSession:
    token: str
    address: str
    created_at: float


class WalletAuthService:
    def __init__(self, nonce_ttl_seconds: int = 300, session_ttl_seconds: int = 86400) -> None:
        self.nonce_ttl_seconds = nonce_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self._nonces: dict[str, tuple[str, float]] = {}
        self._sessions: dict[str, WalletSession] = {}

    def build_message(self, address: str, nonce: str) -> str:
        return (
            "Poybot Wallet Sign-In\n"
            "Sign this message to authenticate your wallet for trading controls.\n\n"
            f"Address: {address.lower()}\n"
            f"Nonce: {nonce}"
        )

    def issue_nonce(self, address: str) -> dict:
        now = time.time()
        nonce = secrets.token_hex(16)
        normalized = address.lower()
        self._nonces[normalized] = (nonce, now + self.nonce_ttl_seconds)
        return {
            "nonce": nonce,
            "message": self.build_message(normalized, nonce),
            "expires_in_seconds": self.nonce_ttl_seconds,
        }

    def verify_signature(self, address: str, signature: str) -> WalletSession:
        normalized = address.lower()
        payload = self._nonces.get(normalized)
        if not payload:
            raise ValueError("missing nonce for this wallet")

        nonce, expiry = payload
        if time.time() > expiry:
            del self._nonces[normalized]
            raise ValueError("nonce expired")

        sig = signature.lower()
        is_hex_prefixed = sig.startswith("0x")
        is_length_ok = len(sig) == 132
        is_hex_chars = all(ch in "0123456789abcdefx" for ch in sig)
        if not (is_hex_prefixed and is_length_ok and is_hex_chars):
            raise ValueError("invalid wallet signature format")

        del self._nonces[normalized]
        token = secrets.token_urlsafe(32)
        session = WalletSession(token=token, address=normalized, created_at=time.time())
        self._sessions[token] = session
        return session

    def get_session(self, token: str) -> WalletSession | None:
        session = self._sessions.get(token)
        if not session:
            return None
        if time.time() > session.created_at + self.session_ttl_seconds:
            del self._sessions[token]
            return None
        return session

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)
