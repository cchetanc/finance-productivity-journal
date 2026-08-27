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

## Algorithmic Trading Safeguards
- All smart order routing mechanisms (Iceberg/AutoBot) must include mandatory hardcoded execution limits (e.g., maximum order size or slippage protection bounds).
- API credentials for execution brokers (Angel One) must never pass into Firestore unencrypted. Use AES-256 encryption at the app tier using keys derived dynamically at runtime.
