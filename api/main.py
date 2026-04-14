import json
import os
import re
from http.server import BaseHTTPRequestHandler

import requests
import yaml


FALLBACK_REPLY = (
    "Merci pour votre message. Je peux vous aider à préparer votre devis peinture. "
    "Pouvez-vous préciser le type de travaux, la surface approximative et votre ville ?"
)


def load_bot_config():
    """Charge la configuration YAML de Betty."""
    base_dir = os.path.dirname(__file__)
    yaml_path = os.path.join(base_dir, "..", "betty_btp_abc.yaml")

    if not os.path.exists(yaml_path):
        return {
            "prompt": (
                "Tu es Betty, assistante commerciale d'une entreprise de peinture au Mans. "
                "Tu aides le visiteur à préparer une demande de devis. "
                "Tu restes concise, claire, polie et orientée prise de contact."
            )
        }

    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def extract_prompt(config):
    """Récupère le prompt depuis le YAML, avec tolérance sur les clés."""
    for key in ["prompt", "system_prompt", "system", "instructions"]:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return (
        "Tu es Betty, assistante d'une entreprise de peinture. "
        "Tu aides à qualifier une demande de devis en posant des questions simples : "
        "type de pièce, surface approximative, état des murs, ville, délai souhaité. "
        "Tu réponds en français, de façon humaine, professionnelle et concise."
    )


def build_messages(system_prompt, user_message):
    """Prépare les messages envoyés au LLM."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def extract_phone(text):
    if not isinstance(text, str):
        return None
    match = re.search(r"(\+33|0)[0-9\s\.-]{8,}", text)
    return match.group(0).strip() if match else None


def send_lead(phone, message):
    """Envoie un lead via Mailjet. Ne lève pas d'exception bloquante."""
    mj_api_key = os.environ.get("MJ_API_KEY", "").strip()
    mj_api_secret = os.environ.get("MJ_API_SECRET", "").strip()
    to_email = os.environ.get("DEFAULT_LEAD_EMAIL", "").strip()
    from_email = os.environ.get("MJ_FROM_EMAIL", "").strip()
    from_name = os.environ.get("MJ_FROM_NAME", "ABC Peinture Déco").strip() or "ABC Peinture Déco"

    if not (mj_api_key and mj_api_secret and to_email and from_email):
        return False, "Mailjet non configuré"

    payload = {
        "Messages": [
            {
                "From": {"Email": from_email, "Name": from_name},
                "To": [{"Email": to_email, "Name": "Client"}],
                "Subject": "🔥 Nouveau lead peinture",
                "TextPart": f"Téléphone: {phone}\n\nMessage: {message}",
            }
        ]
    }

    try:
        res = requests.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(mj_api_key, mj_api_secret),
            json=payload,
            timeout=15,
        )
        res.raise_for_status()
        return True, "Lead envoyé"
    except Exception:
        return False, "Échec envoi lead"


def call_together_api(messages):
    """Appelle Together AI et renvoie une réponse fiable avec fallback."""
    api_key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not api_key:
        return (
            "Service IA temporairement indisponible. "
            "Merci de laisser votre téléphone pour être rappelé sous 48h."
        )

    model = os.environ.get(
        "TOGETHER_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
    ).strip()

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 220,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            "https://api.together.xyz/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=45,
        )
        response.raise_for_status()
        result = response.json() if response.content else {}

        choices = result.get("choices", []) if isinstance(result, dict) else []
        if not choices:
            return FALLBACK_REPLY

        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "").strip() if isinstance(message, dict) else ""
        return content or FALLBACK_REPLY

    except Exception:
        return FALLBACK_REPLY


def json_response(handler, status_code, response_text, extra=None):
    """Envoie une réponse JSON standardisée avec clé 'response' systématique."""
    payload = {"response": response_text}
    if isinstance(extra, dict):
        payload.update(extra)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_json_body(handler):
    """Parsing JSON robuste pour éviter les erreurs serveur."""
    content_length_raw = handler.headers.get("Content-Length", "0")
    try:
        content_length = int(content_length_raw)
    except (TypeError, ValueError):
        content_length = 0

    raw_body = handler.rfile.read(content_length) if content_length > 0 else b"{}"

    if not raw_body:
        return {}

    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    return parsed if isinstance(parsed, dict) else None


class handler(BaseHTTPRequestHandler):
    """Handler Vercel serverless."""

    def do_OPTIONS(self):
        json_response(self, 200, "OK", {"ok": True})

    def do_GET(self):
        json_response(
            self,
            200,
            "API Betty opérationnelle. Utilisez POST sur /api/chat.",
            {"ok": True},
        )

    def do_POST(self):
        """Endpoint principal : reçoit un message utilisateur et répond toujours en JSON."""
        try:
            data = parse_json_body(self)
            if data is None:
                return json_response(
                    self,
                    400,
                    "Requête invalide : JSON incorrect.",
                    {"ok": False},
                )

            user_message = str(data.get("message", "")).strip()
            if not user_message:
                return json_response(self, 400, "Votre message est vide.", {"ok": False})

            phone = extract_phone(user_message)
            if phone:
                send_lead(phone, user_message)

            config = load_bot_config()
            system_prompt = extract_prompt(config)
            messages = build_messages(system_prompt, user_message)
            reply = call_together_api(messages)

            return json_response(self, 200, reply, {"ok": True})

        except Exception:
            return json_response(
                self,
                500,
                "Erreur interne du serveur. Merci de réessayer dans un instant.",
                {"ok": False},
            )
