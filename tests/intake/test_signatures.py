from tenant_hooks.security import signatures

SECRET = b"fake-secret"
TIMESTAMP = "1700000000"


def signature(body: bytes = b"{}", delivery_id: str = "dlv_1") -> str:
    return signatures.sign(SECRET, TIMESTAMP, delivery_id, body)


def test_valid_signature_verifies() -> None:
    assert signatures.verify([SECRET], TIMESTAMP, "dlv_1", b"{}", signature())


def test_any_active_secret_is_accepted() -> None:
    assert signatures.verify([b"old", SECRET], TIMESTAMP, "dlv_1", b"{}", signature())


def test_no_secrets_never_verifies() -> None:
    assert not signatures.verify([], TIMESTAMP, "dlv_1", b"{}", signature())


def test_wrong_secret_fails() -> None:
    assert not signatures.verify([b"other"], TIMESTAMP, "dlv_1", b"{}", signature())


def test_every_signed_field_is_covered() -> None:
    good = signature()
    assert not signatures.verify([SECRET], TIMESTAMP, "dlv_1", b"[]", good)
    assert not signatures.verify([SECRET], TIMESTAMP, "dlv_2", b"{}", good)
    assert not signatures.verify([SECRET], "1700000001", "dlv_1", b"{}", good)


def test_non_ascii_signature_is_rejected_without_error() -> None:
    assert not signatures.verify([SECRET], TIMESTAMP, "dlv_1", b"{}", "v1=é")


def test_timestamp_tolerance_is_inclusive_in_both_directions() -> None:
    assert signatures.timestamp_is_fresh(1000, 1300, 300)
    assert signatures.timestamp_is_fresh(1300, 1000, 300)
    assert not signatures.timestamp_is_fresh(1000, 1301, 300)
    assert not signatures.timestamp_is_fresh(1301, 1000, 300)
