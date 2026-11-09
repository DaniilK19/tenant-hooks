from cryptography.fernet import Fernet


class SecretBox:
    """Encrypts signing secrets at rest. HMAC needs the original secret, so hashing cannot work."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, secret: bytes) -> bytes:
        return self._fernet.encrypt(secret)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)
