# Antigravity Execution Rules

## Development Philosophy
- Prioritise security over speed. Never assume inputs are clean.
- Never write mocks for production database or authentication layers.

## Strict Restrictions
- **Zero Secrets**: Do NOT hardcode API keys, Firebase credentials, or service account files. Always route calls through `backend/app/secrets.py` fetching from Google Cloud Secret Manager.
- **Data Isolation**: Every Firestore operation MUST use the verified `uid` context extracted from the Firebase auth header. Do not accept arbitrary user IDs in the request payload.

## Feature Implementation Pipeline
1. Check threat model in `config/security_constitution.md`.
2. Generate isolated backend controller.
3. Bind UI to the new route using clean component boundaries.
