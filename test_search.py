import urllib.request, json
url = "https://finance-prod-app-backend-36680800010.asia-south1.run.app/api/screener/stocks?search=RELIANCE"
try:
    resp = urllib.request.urlopen(url)
    data = json.loads(resp.read().decode('utf-8'))
    for r in data.get('results', []):
        if r['symbol'] == 'RELIANCE':
            print(json.dumps(r, indent=2))
            break
except Exception as e:
    print("Error:", e)
