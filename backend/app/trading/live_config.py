"""
Live Angel One credentials.

Security model:
  - No credential value is ever hardcoded, logged, persisted to Firestore,
    or accepted as plaintext in an API request body.
  - Environment variables hold only the *name* of a Secret Manager secret
    (e.g. ANGEL_API_KEY_SECRET_ID=angel-one-api-key) — never the secret
    value itself. If an env var leaks (crash dump, debug endpoint, CI log),
    the leak is a secret *name*, which is useless without separate IAM
    access to Secret Manager for that secret.
  - The actual value is fetched from Secret Manager at connect time via
    app/secrets.py's access_secret_version(), the same helper already used
    for BROKER_CREDENTIAL_SALT.
  - Fetched values are cached in memory only, for this process's lifetime.
    They're never written to disk or to any datastore.

Setup (one-time, per environment):
  1. Create four secrets in Secret Manager (values are your real Angel One
     SmartAPI credentials from https://smartapi.angelone.in):
       gcloud secrets create angel-one-api-key       --replication-policy=automatic
       gcloud secrets create angel-one-client-code    --replication-policy=automatic
       gcloud secrets create angel-one-pin            --replication-policy=automatic
       gcloud secrets create angel-one-totp-secret    --replication-policy=automatic
     then add the actual value to each, e.g.:
       echo -n "<your-api-key>" | gcloud secrets versions add angel-one-api-key --data-file=-
  2. Grant the Cloud Run service account read access to each:
       gcloud secrets add-iam-policy-binding angel-one-api-key \
         --member="serviceAccount:<your-run-sa>@<project>.iam.gserviceaccount.com" \
         --role="roles/secretmanager.secretAccessor"
     (repeat for the other three secrets)
  3. Set these env vars on the Cloud Run service (only if your secret names
     differ from the defaults below — otherwise you can skip this step):
       ANGEL_API_KEY_SECRET_ID=angel-one-api-key
       ANGEL_CLIENT_CODE_SECRET_ID=angel-one-client-code
       ANGEL_PIN_SECRET_ID=angel-one-pin
       ANGEL_TOTP_SECRET_SECRET_ID=angel-one-totp-secret
"""
import logging
import os
from dataclasses import dataclass
from threading import Lock

from .. import secrets as app_secrets

logger = logging.getLogger("trading.live_config")

_ENV_DEFAULTS = {
    "api_key": ("ANGEL_API_KEY_SECRET_ID", "angel-one-api-key"),
    "client_code": ("ANGEL_CLIENT_CODE_SECRET_ID", "angel-one-client-code"),
    "pin": ("ANGEL_PIN_SECRET_ID", "angel-one-pin"),
    "totp_secret": ("ANGEL_TOTP_SECRET_SECRET_ID", "angel-one-totp-secret"),
}


@dataclass
class AngelOneCredentials:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str


class MissingCredentialError(Exception):
    """Raised when a required secret isn't configured/accessible yet."""


_cache: AngelOneCredentials | None = None
_cache_lock = Lock()


def _secret_id_for(field_name: str) -> str:
    env_var, default_secret_id = _ENV_DEFAULTS[field_name]
    return os.environ.get(env_var, default_secret_id)


def _fetch_field(field_name: str) -> str:
    secret_id = _secret_id_for(field_name)
    try:
        return app_secrets.access_secret_version(secret_id)
    except Exception as e:  # noqa: BLE001 - surface as a clear, actionable error
        raise MissingCredentialError(
            f"Could not read '{field_name}' from Secret Manager secret '{secret_id}'. "
            f"Confirm the secret exists and the service account has "
            f"roles/secretmanager.secretAccessor on it. ({e})"
        ) from e


def get_angel_one_live_credentials(force_refresh: bool = False) -> AngelOneCredentials:
    """
    Returns the live Angel One credentials, fetched from Secret Manager and
    cached in memory for this process. Never returns a hardcoded value and
    never writes the result anywhere persistent.
    """
    global _cache
    with _cache_lock:
        if _cache is not None and not force_refresh:
            return _cache
        creds = AngelOneCredentials(
            api_key=_fetch_field("api_key"),
            client_code=_fetch_field("client_code"),
            pin=_fetch_field("pin"),
            totp_secret=_fetch_field("totp_secret"),
        )
        _cache = creds
        logger.info(
            "Loaded live Angel One credentials from Secret Manager (client_code=%s...)",
            creds.client_code[:2] + "***" if creds.client_code else "?",
        )
        return creds


def clear_cache():
    """Drops the in-memory cache, forcing the next call to re-fetch from
    Secret Manager (e.g. after rotating a secret)."""
    global _cache
    with _cache_lock:
        _cache = None