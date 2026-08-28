import requests

url = "https://finance-prod-app-backend-36680800010.asia-south1.run.app/api/market/voice"
payload = {
    "prompt": "hi",
    "persona": "Aoede",
    "session_id": "demo_test"
}
resp = requests.post(url, json=payload)
print("Status:", resp.status_code)
if resp.status_code == 200:
    data = resp.json()
    print("Text:", data.get("text"))
    if "audio_base64" in data:
        print("Audio Base64 length:", len(data["audio_base64"]))
    else:
        print("No audio data")
else:
    print("Error:", resp.text)
