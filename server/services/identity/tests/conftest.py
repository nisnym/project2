"""Fixtures for identity tests.

RSA-2048 keygen costs ~0.5s and the service generates a key on first use, so a
naive suite pays it once per test. One keypair is generated per session and
reused; each call still gets a distinct kid, so key-rotation tests remain
meaningful.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest


@pytest.fixture(scope="session")
def rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


@pytest.fixture(autouse=True)
def fast_keygen(monkeypatch, rsa_keypair):
    private_pem, public_pem = rsa_keypair

    def _generate_key():
        from identity.models import SigningKey

        # Distinct kid per call so rotation still produces a different key id.
        kid = hashlib.sha256(f"{public_pem}{uuid.uuid4()}".encode()).hexdigest()[:16]
        return SigningKey.objects.create(
            kid=kid, public_pem=public_pem, private_pem=private_pem
        )

    monkeypatch.setattr("identity.services.generate_key", _generate_key)
