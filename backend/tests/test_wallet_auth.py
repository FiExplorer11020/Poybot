from app.services.wallet_auth import WalletAuthService


def test_nonce_and_signature_roundtrip() -> None:
    svc = WalletAuthService(nonce_ttl_seconds=60)
    address = "0xA11ce00000000000000000000000000000B0B01"
    svc.issue_nonce(address)

    session = svc.verify_signature(address, "0x" + "a" * 130)

    assert session.address == address.lower()
    assert svc.get_session(session.token) is not None


def test_invalid_signature_rejected() -> None:
    svc = WalletAuthService(nonce_ttl_seconds=60)
    address = "0xA11ce00000000000000000000000000000B0B01"
    svc.issue_nonce(address)

    try:
        svc.verify_signature(address, "not-a-signature")
        assert False, "expected signature rejection"
    except ValueError as exc:
        assert "invalid wallet signature format" in str(exc)
