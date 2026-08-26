# Secure Architecture Blueprint: Personal Gemini Journal

This document describes the end-to-end integration and data lifecycle flow for the Ideathon 3 application, adhering strictly to enterprise security guardrails.

## 1. High-Level Authentication & Authorization Flow
1. **Client-Side Auth**: The user signs in via the Frontend using Firebase Authentication (Google Auth / Email Provider).
2. **Token Generation**: Firebase produces a short-lived JSON Web Token (JWT) ID token on the client.
3. **API Request**: The frontend attaches this token inside the `Authorization: Bearer <Token>` header for all backend requests.
4. **Backend Verification**: The Python backend pulls the public keys from Firebase Admin SDK to decode and verify the token, securely extracting the user's explicit `uid`.

## 2. Dynamic Secret Retrieval Flow
To prevent hardcoded vulnerabilities, the Gemini API Key is never loaded into environmental variables permanently or checked into source code.
1. Backend calls Google Cloud Secret Manager API at runtime (`SecretManagerServiceClient`).
2. It requests the payload for `projects/YOUR_PROJECT_ID/secrets/GEMINI_API_KEY/versions/latest`.
3. The key is held strictly in-memory during execution blocks and dropped immediately after API initialization.

## 3. Data Isolation Matrix
* **Primary Keying**: Every collection path is structured hierarchically using the authenticated identity: `/users/{uid}/journals/{journal_id}`
* **Summarization Process**: When a multi-turn journal conversation completes, a background worker requests a concise summary from Gemini 3.5 Flash and writes the data payload directly to the user's isolated Firestore cluster.
