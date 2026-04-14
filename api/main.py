import os
import json
import re
import requests
from http.server import BaseHTTPRequestHandler

# ===== CONFIG =====
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")

MJ_API_KEY = os.getenv("MJ_API_KEY")
MJ_API_SECRET = os.getenv("MJ_API_SECRET")
MJ_FROM_EMAIL = os.getenv("MJ_FROM_EMAIL")
DEFAULT_LEAD_EMAIL = os.getenv("DEFAULT_LEAD_EMAIL")

# ===== SESSION =====
sessions = {}

# ===== FLOW =====
FLOW = ["projet", "surface", "delai", "nom", "telephone", "email"]

QUESTIONS = {
    "projet": "Quelle pièce souhaitez-vous refaire ?",
    "surface": "Quelle surface environ en m² ?",
    "delai": "Vous souhaitez faire les travaux quand ?",
    "nom": "Quel est votre prénom ?",
    "telephone": "Quel est votre numéro de téléphone ?",
    "email": "Quel est votre email ?"
}

# ===== JSON RESPONSE =====
def json_response(handler, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

# ===== NORMALISATION =====
def normalize_input(message, step):
    # 👇 LE FIX CRUCIAL
    if step == "surface" and message.isdigit():
        return f"{message} m2"
    return message

# ===== ENVOI LEAD =====
def send_lead(data):
    if not (MJ_API_KEY and MJ_API_SECRET and DEFAULT_LEAD_EMAIL):
        print("MAILJET NON CONFIGURÉ")
        return

    try:
        requests.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(MJ_API_KEY, MJ_API_SECRET),
            json={
                "Messages": [
                    {
                        "From": {
                            "Email": MJ_FROM_EMAIL,
                            "Name": "Betty"
                        },
                        "To": [{"Email": DEFAULT_LEAD_EMAIL}],
                        "Subject": "🔥 Nouveau lead peinture",
                        "TextPart": json.dumps(data, indent=2, ensure_ascii=False)
                    }
                ]
            }
        )
    except Exception as e:
        print("ERREUR MAIL:", e)

# ===== LLM =====
def call_llm(user_msg, context, current_step, next_question):
    if not TOGETHER_API_KEY:
        return next_question

    try:
        prompt = f"""
Tu es Betty, assistante commerciale pour une entreprise de peinture.

IMPORTANT :
Le client répond à cette question :
{current_step}

Sa réponse :
{user_msg}

Contexte :
{json.dumps(context, ensure_ascii=False)}

Ton comportement :
- comprendre la réponse même si elle est courte (ex: "100")
- valider rapidement (ex: "Parfait")
- poser la question suivante

Règles :
- 1 phrase courte
- naturel
- jamais répéter
- jamais reposer la même question

Question suivante :
{next_question}

Réponse :
"""

        r = requests.post(
            "https://api.together.xyz/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {TOGETHER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "Assistant commercial BTP"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.5,
                "max_tokens": 80
            },
            timeout=20
        )

        data = r.json()
        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("ERREUR LLM:", e)
        return next_question

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

            if not message:
                return json_response(self, {
                    "response": "Pouvez-vous préciser votre demande ?"
                })

            user_id = self.client_address[0]

            if user_id not in sessions:
                sessions[user_id] = {"step": 0, "data": {}}

            s = sessions[user_id]

            # étape actuelle
            if s["step"] < len(FLOW):
                current_key = FLOW[s["step"]]
            else:
                current_key = None

            # normalisation
            message = normalize_input(message, current_key)

            # sauvegarde
            if current_key:
                s["data"][current_key] = message
                s["step"] += 1

            # FIN
            if s["step"] >= len(FLOW):
                send_lead(s["data"])
                return json_response(self, {
                    "response": "Parfait 👍 On vous recontacte très rapidement."
                })

            # prochaine question
            next_key = FLOW[s["step"]]
            next_q = QUESTIONS[next_key]

            # réponse LLM intelligente
            reply = call_llm(message, s["data"], current_key, next_q)

            return json_response(self, {
                "response": reply
            })

        except Exception as e:
            return json_response(self, {
                "response": "Erreur serveur",
                "debug": str(e)
            })
