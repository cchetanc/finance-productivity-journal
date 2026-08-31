"""
Run this locally (where you have real access to Secret Manager / the key)
to see exactly which Gemini models your API key can call right now.

Usage:
    GEMINI_API_KEY=your-key python3 list_available_models.py

or, if you'd rather pull straight from Secret Manager like the app does:
    python3 list_available_models.py --from-secret-manager
"""
import os
import sys
import argparse

from google import genai


def get_api_key(args):
    if args.from_secret_manager:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
        from app.secrets import access_secret_version
        return access_secret_version("GEMINI_API_KEY")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Set GEMINI_API_KEY env var, or pass --from-secret-manager")
    return key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-secret-manager", action="store_true")
    args = parser.parse_args()

    client = genai.Client(api_key=get_api_key(args))

    print(f"{'MODEL NAME':45} SUPPORTS generateContent?")
    print("-" * 70)
    usable = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        supports_generate = "generateContent" in actions
        if supports_generate:
            usable.append(m.name)
        print(f"{m.name:45} {'YES' if supports_generate else 'no'}")

    print("\nModels you can actually use for chat/generation:")
    for name in usable:
        print(f"  - {name}")

    if usable:
        print(f"\nSuggested value for GEMINI_MODEL env var: {usable[0].replace('models/', '')}")


if __name__ == "__main__":
    main()