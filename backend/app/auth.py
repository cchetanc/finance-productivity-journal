from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import firebase_admin
from firebase_admin import credentials, auth

# Initialize Firebase Admin if not already initialized
if not firebase_admin._apps:
    # Use default credentials for the database (gen-lang-client...)
    default_app = firebase_admin.initialize_app()
    # Create a secondary app specifically for authenticating tokens from the frontend's Firebase project
    auth_app = firebase_admin.initialize_app(options={"projectId": "my-finance-terminal-auth"}, name="frontend_auth")

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    Verify Firebase ID token from the Authorization header.
    Returns the decoded token containing the user's uid.
    """
    token = credentials.credentials
    try:
        # Verify using the specific app instance bound to the frontend's Firebase project
        decoded_token = auth.verify_id_token(token, app=firebase_admin.get_app("frontend_auth"))
        uid = decoded_token.get("uid")
        if not uid:
            raise ValueError("Token does not contain uid")
        return {"uid": uid, "decoded_token": decoded_token}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication credentials: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

def get_current_user_uid(auth_data: dict = Depends(verify_token)) -> str:
    """
    Dependency to extract and inject the verified uid into the request lifecycle.
    """
    return auth_data["uid"]


def get_current_user(auth_data: dict = Depends(verify_token)) -> dict:
    """
    Dependency returning {"uid": ..., "email": ...} — the email comes
    straight from the verified Firebase ID token's claims, never from a
    client-supplied request field, so it can't be spoofed by the caller.
    Used anywhere (e.g. routers/auth.py's Gmail-domain check) that needs
    the authenticated user's email, not just their uid.
    """
    decoded = auth_data["decoded_token"]
    return {"uid": auth_data["uid"], "email": decoded.get("email")}