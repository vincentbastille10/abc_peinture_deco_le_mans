import os
import yaml
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# CONFIG LLM
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")
TOGETHER_API_URL = "https://api.together.xyz/v1/chat/completions"
MODEL = "meta-llama/Meta-Llama-3-8B-Instruct-Turbo"


# ─────────────────────────────
# LOAD YAML
# ─────────────────────────────
def load_bot_config(bot_id):
    with open(f"{bot_id}.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────
# BUILD PROMPT
# ─────────────────────────────
def build_prompt(bot_config, user_message):
    return f"""
Tu es {bot_config['name']}, {bot_config['role']}.

Objectif:
{bot_config['main_goal']}

Règles:
{chr(10).join(bot_config['rules'])}

Stratégie:
{bot_config.get('conversation_strategy', '')}

Exemples:
{bot_config.get('conversation_examples', '')}

Message utilisateur:
{user_message}

Réponds de manière naturelle, courte et professionnelle.
"""


# ─────────────────────────────
# CALL LLM
# ─────────────────────────────
def call_llm(prompt):
    headers = {
        "Authorization": f"Bearer {TOGETHER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": prompt}
        ],
        "temperature": 0.7
    }

    response = requests.post(TOGETHER_API_URL, json=payload, headers=headers)
    data = response.json()

    return data["choices"][0]["message"]["content"]


# ─────────────────────────────
# API ROUTE
# ─────────────────────────────
@app.route("/api/bettybot", methods=["POST"])
def bettybot():
    data = request.json

    user_message = data.get("message")
    bot_id = data.get("bot_id", "betty_btp_abc")

    bot_config = load_bot_config(bot_id)

    prompt = build_prompt(bot_config, user_message)
    response = call_llm(prompt)

    return jsonify({"response": response})


# ─────────────────────────────
# RUN
# ─────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)
