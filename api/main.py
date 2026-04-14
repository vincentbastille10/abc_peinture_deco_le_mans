import os
from pathlib import Path

import requests
import yaml
from flask import Flask, jsonify, request

app = Flask(__name__)

TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")
TOGETHER_API_URL = "https://api.together.xyz/v1/chat/completions"
MODEL = os.getenv("TOGETHER_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")
BOT_CONFIG_PATH = Path(__file__).resolve().parent / "betty_btp_abc.yaml"


def load_bot_config():
    with BOT_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def build_system_prompt(bot_config):
    name = bot_config.get("name", "Betty")
    role = bot_config.get("role", "Assistante commerciale")
    main_goal = bot_config.get("main_goal", "Aider les visiteurs")
    rules = bot_config.get("rules", [])
    closing_message = bot_config.get("closing_message", "")
    example_projects = bot_config.get("example_projects", [])

    rules_block = "\n".join(f"- {rule}" for rule in rules) if rules else "- Répondez de manière utile."
    projects_block = ", ".join(example_projects) if example_projects else "peinture intérieure, peinture extérieure"

    return (
        f"Tu es {name}, {role}.\n\n"
        f"Objectif principal :\n{main_goal}\n\n"
        f"Règles à respecter :\n{rules_block}\n\n"
        f"Exemples de projets traités : {projects_block}.\n"
        f"Message de clôture attendu si les coordonnées sont collectées : {closing_message}\n\n"
        "Réponds toujours en français, avec un ton professionnel, rassurant et concret."
    )


def call_together(messages):
    if not TOGETHER_API_KEY:
        raise RuntimeError("TOGETHER_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {TOGETHER_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.4,
    }

    response = requests.post(TOGETHER_API_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        body = request.get_json(silent=True) or {}
        user_message = (body.get("message") or "").strip()

        if not user_message:
            return jsonify({"error": "Le champ 'message' est requis."}), 400

        bot_config = load_bot_config()
        system_prompt = build_system_prompt(bot_config)

        history = body.get("history", [])
        safe_history = [
            item for item in history
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
        ]

        messages = [{"role": "system", "content": system_prompt}, *safe_history, {"role": "user", "content": user_message}]
        assistant_reply = call_together(messages)

        return jsonify({"response": assistant_reply})
    except requests.HTTPError as exc:
        details = exc.response.text[:500] if exc.response is not None else str(exc)
        return jsonify({"error": "Erreur Together API", "details": details}), 502
    except FileNotFoundError:
        return jsonify({"error": "Configuration bot introuvable (betty_btp_abc.yaml)."}), 500
    except Exception as exc:
        return jsonify({"error": "Erreur interne", "details": str(exc)}), 500


@app.route("/", methods=["GET"])
def healthcheck():
    return jsonify({"status": "ok", "service": "betty-api"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
