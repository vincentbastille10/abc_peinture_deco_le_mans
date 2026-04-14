import os
import json
import requests
from http.server import BaseHTTPRequestHandler

TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    handler.wfile.write(body)


def call_llm(message):
    if not TOGETHER_API_KEY:
        return "⚠️ Clé API manquante."

    try:
        res = requests.post(
            "https://api.together.xyz/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {TOGETHER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Tu es Betty, assistante pour une entreprise de peinture.\n"
                            "Tu aides à préparer un devis.\n"
                            "Tu parles français.\n"
                            "Tu poses UNE question à la fois.\n"
                            "Tu es courte, naturelle et efficace.\n"
                        )
                    },
                    {"role": "user", "content": message}
                ],
                "temperature": 0.5,
                "max_tokens": 200
            },
            timeout=30
        )

        data = res.json()

        print("LLM RAW:", data)  # 🔥 LOG IMPORTANT

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("ERREUR LLM:", str(e))
        return "Je peux vous aider à préparer votre devis. Quelle pièce souhaitez-vous refaire ?"


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        return json_response(self, 200, {"ok": True})

    def do_GET(self):
        return json_response(self, 200, {
            "ok": True,
            "response": "API OK - utilisez POST"
        })

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            data = json.loads(body.decode("utf-8"))

            message = data.get("message", "").strip()

            if not message:
                return json_response(self, 200, {
                    "response": "Pouvez-vous préciser votre demande ?"
                })

            reply = call_llm(message)

            return json_response(self, 200, {
                "response": reply
            })

        except Exception as e:
            print("ERREUR GLOBAL:", str(e))

            return json_response(self, 200, {
                "response": "Erreur interne du serveur.",
                "debug": str(e)
            })
