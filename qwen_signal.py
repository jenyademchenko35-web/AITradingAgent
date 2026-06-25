import requests

prompt = """
Цена BTC: 62039
RSI: 23.99
EMA20: 63193
EMA50: 63673

Дай ответ строго в формате:

SIGNAL: LONG/SHORT/NO TRADE
CONFIDENCE: %
REASON:
"""

response = requests.post(
    "http://localhost:11434/api/generate",
    json={
        "model": "qwen3:14b",
        "prompt": prompt,
        "stream": False
    }
)

print(response.json()["response"])