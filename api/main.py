import os
import json
import requests
from http.server import BaseHTTPRequestHandler

TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")

# ===== FLOW QUALIFICATION =====
# Ordre : projet → surface → delai → prenom → telephone → email
FLOW = ["projet", "surface", "delai", "prenom", "telephone", "email"]

QUESTIONS = {
    "projet": "Bonjour ! Je suis Betty 👋 l'assistante d'ABC Peinture Déco. Pour préparer votre devis, quel type de travaux souhaitez-vous réaliser ?",
    "surface": "Quelle surface approximative en m² ?",
    "delai": "Vous souhaitez réaliser les travaux quand ?",
    "prenom": "Parfait, j'ai bien noté votre projet 👍 Quel est votre prénom ?",
    "telephone": "Votre numéro de téléphone ?",
    "email": "Et votre adresse email pour recevoir le devis ?"
}

CLOSING_TEMPLATE = (
    "Merci {prenom} ! Votre demande est bien enregistrée 🎉 "
    "L'équipe d'ABC Peinture Déco vous rappelle rapidement pour finaliser votre devis."
)

GREETINGS = {"bonjour", "bonsoir", "salut", "hello", "hi", "coucou", "bjr", "bsr", "yo", "bj", "ok", "oui"}

sessions = {}


def is_greeting(message):
    """Vérifie si le message est juste une salutation."""
    clean = message.lower().strip().rstrip("!").rstrip(".").rstrip(",")
    return clean in GREETINGS


def get_question(key, data):
    """Retourne la question, personnalisée avec le prénom si disponible."""
    q = QUESTIONS[key]
    if "{prenom}" in q and "prenom" in data:
        q = q.format(prenom=data["prenom"])
    return q


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
        context_str = ""
        if context.get("projet"):
            context_str += f"- Projet : {context['projet']}\n"
        if context.get("surface"):
            context_str += f"- Surface : {context['surface']}\n"
        if context.get("delai"):
            context_str += f"- Délai souhaité : {context['delai']}\n"
        if context.get("prenom"):
            context_str += f"- Prénom client : {context['prenom']}\n"

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
                            "Tu es Betty, assistante commerciale pour ABC Peinture Déco au Mans (Sarthe).\n"
                            "Tu réponds aux questions clients de façon naturelle, chaleureuse et concise.\n"
                            "Tu utilises le prénom du client quand il est disponible.\n"
                            "Réponses courtes, max 2-3 phrases. Une seule question à la fois si besoin."
                        )
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Contexte du projet client :\n{context_str}\n\n"
                            f"Question du client : {message}\n\n"
                            "Réponds simplement et chaleureusement."
                        )
                    }
                ],
                "temperature": 0.7,
                "max_tokens": 180
            },
            timeout=20
        )
        return r.json()["choices"][0]["message"]["content"]

    except Exception:
        return (
            "Je suis là pour vous aider ! N'hésitez pas à appeler le 02 43 75 98 18 "
            "si vous préférez échanger directement."
        )


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

            # Initialise la session
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

                # Si c'est une salutation en début de conversation → on répond chaleureusement
                # sans enregistrer le message comme donnée
                if step == 0 and is_greeting(message):
                    return json_response(self, {
                        "response": QUESTIONS["projet"]
                    })

                key = FLOW[step]

                # Normalise le prénom
                if key == "prenom":
                    message = message.strip().capitalize()

                s["data"][key] = message
                s["step"] += 1

                # Micro-réactions chaleureuses après chaque étape
                warmth = ""
                if key == "prenom":
                    warmth = f"Enchanté {message} ! "
                elif key == "projet":
                    warmth = "Super, j'ai noté ! "
                elif key == "surface":
                    warmth = "Merci ! "
                elif key == "delai":
                    warmth = "Parfait ! "
                elif key == "telephone":
                    warmth = "Noté 👍 "

                # Encore des infos à demander
                if s["step"] < len(FLOW):
                    next_key = FLOW[s["step"]]
                    next_q = get_question(next_key, s["data"])
                    response = (warmth + next_q) if warmth else next_q
                    return json_response(self, {"response": response})

                # FIN qualification → message de closing
                s["qualified"] = True
                prenom = s["data"].get("prenom", "")
                closing = (
                    CLOSING_TEMPLATE.format(prenom=prenom)
                    if prenom
                    else (
                        "Merci ! Votre demande est bien enregistrée 🎉 "
                        "L'équipe d'ABC Peinture Déco vous rappelle rapidement."
                    )
                )
                return json_response(self, {"response": closing})

            # ===== PHASE 2 : RÉPONSES INTELLIGENTES =====
            reply = call_llm(message, s["data"])
            return json_response(self, {"response": reply})

        except Exception as e:
            return json_response(self, {
                "response": (
                    "Une petite erreur s'est glissée ! "
                    "Appelez-nous au 02 43 75 98 18, on sera ravis de vous aider."
                ),
                "debug": str(e)
            })
