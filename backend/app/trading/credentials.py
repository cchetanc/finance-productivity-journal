"""
Per-user broker credential storage.

Angel One credentials (api_key, client_code, pin, totp_secret) are sensitive
in the same way a password is: never log them, never return them from an API
response, never store them in plaintext. This module encrypts them at rest
in Firestore using a key derived from the BROKER_CREDENTIAL_SALT secret that
already exists in app/secrets.py, combined with a per-user random salt.
"""
import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from .. import secrets as app_secrets
from ..database import db


@dataclass
class AngelOneCredentials:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str


def _derive_key(uid: str) -> bytes:
    server_salt = app_secrets.get_broker_credential_salt()
    material = hashlib.sha256(f"{server_salt}:{uid}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(material)


def _fernet_for(uid: str) -> Fernet:
    return Fernet(_derive_key(uid))


def _creds_ref(uid: str):
    return db.collection("users").document(uid).collection("config").document("angel_one_credentials")


def save_angel_one_credentials(uid: str, creds: AngelOneCredentials):
    f = _fernet_for(uid)
    encrypted = {
        "api_key": f.encrypt(creds.api_key.encode()).decode(),
        "client_code": f.encrypt(creds.client_code.encode()).decode(),
        "pin": f.encrypt(creds.pin.encode()).decode(),
        "totp_secret": f.encrypt(creds.totp_secret.encode()).decode(),
    }
    _creds_ref(uid).set(encrypted)


def get_angel_one_credentials(uid: str) -> AngelOneCredentials | None:
    doc = _creds_ref(uid).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    f = _fernet_for(uid)
    try:
        return AngelOneCredentials(
            api_key=f.decrypt(data["api_key"].encode()).decode(),
            client_code=f.decrypt(data["client_code"].encode()).decode(),
            pin=f.decrypt(data["pin"].encode()).decode(),
            totp_secret=f.decrypt(data["totp_secret"].encode()).decode(),
        )
    except InvalidToken as e:
        raise ValueError("Stored broker credentials could not be decrypted") from e


def delete_angel_one_credentials(uid: str):
    _creds_ref(uid).delete()