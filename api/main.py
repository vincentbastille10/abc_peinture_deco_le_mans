import os
import json
from http.server import BaseHTTPRequestHandler

import requests
import yaml


def load_bot_config():
    """
    Charge la config YAML de Betty.
    """
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
        data = yaml.safe_load(f) or {}

    return data


def extract_prompt(config):
    """
    Récupère le prompt depuis le YAML.
    On tolère plusieurs noms de clés pour éviter les bugs.
    """
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
    """
    Prépare les messages envoyés au LLM.
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def call_together_api(messages):
    """
    Appelle Together AI et renvoie le texte de réponse.
    """
    api_key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not api_key:
        return (
            "La clé Together API n'est pas configurée sur Vercel. "
            "Ajoutez TOGETHER_API_KEY dans les variables d'environnement."
        )

    model = os.environ.get(
        "TOGETHER_MODEL",
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
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

    response = requests.post(
        "https://api.together.xyz/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=45
    )

    response.raise_for_status()
    result = response.json()

    choices = result.get("choices", [])
    if not choices:
        return "Je n’ai pas pu générer de réponse pour le moment. Pouvez-vous reformuler ?"

    message = choices[0].get("message", {})
    content = message.get("content", "").strip()

    if not content:
        return "Je n’ai pas pu générer de réponse pour le moment. Pouvez-vous reformuler ?"

    return content


def json_response(handler, status_code, payload):
    """
    Envoie une réponse JSON propre.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    """
    Handler Vercel serverless.
    """

    def do_OPTIONS(self):
        json_response(self, 200, {"ok": True})

    def do_GET(self):
        json_response(
            self,
            200,
            {
                "ok": True,
                "message": "API Betty opérationnelle. Utilisez POST sur /api/chat."
            }
        )

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

            try:
                data = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                return json_response(
                    self,
                    400,
                    {"response": "Requête invalide : JSON incorrect."}
                )

            user_message = str(data.get("message", "")).strip()
            if not user_message:
                return json_response(
                    self,
                    400,
                    {"response": "Votre message est vide."}
                )

            config = load_bot_config()
            system_prompt = extract_prompt(config)
            messages = build_messages(system_prompt, user_message)
            reply = call_together_api(messages)

            return json_response(
                self,
                200,
                {"response": reply}
            )

        except requests.exceptions.Timeout:
            return json_response(
                self,
                504,
                {"response": "Le service met trop de temps à répondre. Réessayez dans un instant."}
            )

        except requests.exceptions.HTTPError as e:
            status = 500
            details = ""

            if hasattr(e, "response") and e.response is not None:
                status = e.response.status_code
                try:
                    details = e.response.text[:500]
                except Exception:
                    details = ""

            return json_response(
                self,
                500,
                {
                    "response": "Erreur lors de l'appel au modèle IA.",
                    "details": details,
                    "upstream_status": status
                }
            )

        except Exception as e:
            return json_response(
                self,
                500,
                {
                    "response": "Erreur interne du serveur.",
                    "details": str(e)
                }
            )
