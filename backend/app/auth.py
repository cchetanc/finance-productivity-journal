from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import firebase_admin
from firebase_admin import credentials, auth

# Initialize Firebase Admin if not already initialized
if not firebase_admin._apps:
    # Use default credentials (e.g., in Cloud Run) or a service account 
    # injected securely, NEVER hardcoded.
    default_app = firebase_admin.initialize_app()

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    Verify Firebase ID token from the Authorization header.
    Returns the decoded token containing the user's uid.
    """
    token = credentials.credentials
    try:
        decoded_token = auth.verify_id_token(token)
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
