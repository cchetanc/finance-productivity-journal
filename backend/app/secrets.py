import os
import google.auth
from google.cloud import secretmanager

try:
    _, PROJECT_ID = google.auth.default()
except Exception:
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-project-id")

def access_secret_version(secret_id, version_id="latest"):
    """
    Access the payload of the given secret version dynamically.
    No secrets are cached locally or written to files.
    """
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/{version_id}"
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")
    return payload

def get_gemini_api_key() -> str:
    return access_secret_version("GEMINI_API_KEY")

def get_broker_credential_salt() -> str:
    """
    Retrieves the salt used for decrypting broker credentials in-memory.
    """
    return access_secret_version("BROKER_CREDENTIAL_SALT")
