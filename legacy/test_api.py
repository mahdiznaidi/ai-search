import os
import requests

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # export OPENROUTER_API_KEY="..."
if not OPENROUTER_API_KEY:
    raise SystemExit("❌ Manquant: export OPENROUTER_API_KEY='...'\n")

URL = "https://openrouter.ai/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    # Recommandé par OpenRouter pour l'analytics/rate-limit (mets ton domaine et/ou email)
    "HTTP-Referer": "http://localhost",
    "X-Title": "openrouter-o4mini-test",
}

payload = {
    "model": "openai/o4-mini",
    "messages": [
        {"role": "system", "content": "Tu es un assistant utile."},
        {"role": "user", "content": "Dis-moi bonjour en une phrase courte."}
    ],
    "temperature": 0.2,
}

resp = requests.post(URL, headers=headers, json=payload, timeout=60)

print("Status:", resp.status_code)
try:
    data = resp.json()
except Exception:
    print(resp.text)
    raise

if resp.status_code != 200:
    print("Erreur:", data)
    raise SystemExit(1)

# Affiche la réponse
print("\n--- Réponse modèle ---")
print(data["choices"][0]["message"]["content"])

# Optionnel: usage si présent
usage = data.get("usage")
if usage:
    print("\n--- Usage ---")
    print(usage)
