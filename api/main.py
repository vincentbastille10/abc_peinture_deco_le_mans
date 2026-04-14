import os
import json
import requests
from http.server import BaseHTTPRequestHandler

TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")

# ===== FLOW =====
FLOW = ["prenom", "nom", "telephone", "email"]

QUESTIONS = {
    "prenom": "Pour commencer, quel est votre prénom ?",
    "nom": "Merci 👍 Et votre nom ?",
    "telephone": "Votre numéro de téléphone ?",
    "email": "Votre email ?"
}

sessions = {}

# ===== JSON =====
def json_response(handler, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

# ===== LLM =====
def call_llm(message, context):
    try:
        r = requests.post(
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
                            "Tu réponds aux questions des clients.\n"
                            "Tu es courte, naturelle et efficace.\n"
                        )
                    },
                    {
                        "role": "user",
                        "content": f"""
Client :
{json.dumps(context, ensure_ascii=False)}

Question :
{message}

Réponds simplement et clairement.
"""
                    }
                ],
                "temperature": 0.5,
                "max_tokens": 150
            },
            timeout=20
        )

        return r.json()["choices"][0]["message"]["content"]

    except:
        return "Je peux vous aider pour votre devis. Pouvez-vous préciser votre besoin ?"

# ===== HANDLER =====
class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        return json_response(self, {"ok": True})

    def do_GET(self):
        return json_response(self, {"ok": True})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))

            message = data.get("message", "").strip()

            user_id = self.client_address[0]

            if user_id not in sessions:
                sessions[user_id] = {
                    "step": 0,
                    "data": {},
                    "qualified": False
                }

            s = sessions[user_id]

            # ===== PHASE 1 : QUALIFICATION =====
            if not s["qualified"]:

                step = s["step"]
                key = FLOW[step]

                s["data"][key] = message
                s["step"] += 1

                # encore des infos à demander
                if s["step"] < len(FLOW):
                    next_key = FLOW[s["step"]]
                    return json_response(self, {
                        "response": QUESTIONS[next_key]
                    })

                # FIN qualification
                s["qualified"] = True

                return json_response(self, {
                    "response": "Parfait 👍 Je peux maintenant répondre à vos questions concernant votre projet."
                })

            # ===== PHASE 2 : RÉPONSES INTELLIGENTES =====
            reply = call_llm(message, s["data"])

            return json_response(self, {
                "response": reply
            })

        except Exception as e:
            return json_response(self, {
                "response": "Erreur serveur",
                "debug": str(e)
            })
