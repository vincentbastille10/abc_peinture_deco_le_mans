import os
import json
import re
import requests
from http.server import BaseHTTPRequestHandler

# ===== ENV =====
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
    "nom": "À quel prénom puis-je enregistrer votre demande ?",
    "telephone": "Quel est votre numéro de téléphone ?",
    "email": "Et votre email ?"
}

# ===== SCORING =====
def score_lead(data):
    score = 0
    if data.get("projet"): score += 1
    if data.get("surface"): score += 1
    if data.get("delai"): score += 2
    if data.get("telephone"): score += 3
    if data.get("email"): score += 2

    if score >= 6:
        return "chaud 🔥"
    elif score >= 3:
        return "tiède"
    return "froid"

# ===== CTA =====
def build_cta(data):
    score = score_lead(data)

    if score == "chaud 🔥":
        return "📞 Appelez directement le 02 43 75 98 18 pour aller plus vite."

    if data.get("delai"):
        return "📞 Vous pouvez appeler le 02 43 75 98 18 pour accélérer."

    return ""

# ===== RESPONSE JSON =====
def json_response(handler, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

# ===== EXTRACTION =====
def extract_lead(text):
    email = re.findall(r"\S+@\S+\.\S+", text)
    phone = re.findall(r"\b\d{10}\b", text)

    return {
        "email": email[0] if email else None,
        "phone": phone[0] if phone else None
    }

# ===== SEND MAIL =====
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
            },
            timeout=15
        )
    except Exception as e:
        print("ERREUR MAIL:", e)

# ===== LLM =====
def call_llm(user_msg, context, next_question):
    if not TOGETHER_API_KEY:
        return next_question

    try:
        cta = build_cta(context)

        prompt = f"""
Tu es Betty, assistante commerciale pour une entreprise de peinture.

Règles :
- réponse très courte
- naturelle
- pas robotique
- jamais répéter
- poser UNE seule question
- guider vers devis

Contexte :
{json.dumps(context, ensure_ascii=False)}

Utilisateur : {user_msg}

Question suivante :
{next_question}

CTA si pertinent :
{cta}

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
                "temperature": 0.7,
                "max_tokens": 120
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

            # extraction auto
            auto = extract_lead(message)
            if auto["email"]:
                s["data"]["email"] = auto["email"]
            if auto["phone"]:
                s["data"]["telephone"] = auto["phone"]

            # skip étapes déjà remplies
            while s["step"] < len(FLOW) and FLOW[s["step"]] in s["data"]:
                s["step"] += 1

            # sauvegarde
            if s["step"] < len(FLOW):
                s["data"][FLOW[s["step"]]] = message
                s["step"] += 1

            # FIN
            if s["step"] >= len(FLOW):
                send_lead(s["data"])

                score = score_lead(s["data"])

                if score == "chaud 🔥":
                    return json_response(self, {
                        "response": "Parfait 👍 Votre demande est prioritaire. Appelez directement le 02 43 75 98 18."
                    })

                return json_response(self, {
                    "response": "Parfait 👍 On vous recontacte rapidement pour votre devis."
                })

            # suite
            next_key = FLOW[s["step"]]
            next_q = QUESTIONS[next_key]

            reply = call_llm(message, s["data"], next_q)

            return json_response(self, {
                "response": reply
            })

        except Exception as e:
            return json_response(self, {
                "response": "Erreur serveur",
                "debug": str(e)
            })
