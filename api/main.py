"""
Betty — ABC Peinture Déco
Backend Vercel serverless (drop-in remplaçant de api/main.py)

Ce qui change vs. la version actuelle :
  - Le YAML est VRAIMENT chargé et piloté depuis ce fichier
  - classify_message() : pertinent / flou / hors_sujet / info_hors_flow / greeting
  - Validation par étape (téléphone, email, surface, prénom)
  - Recadrages à variantes (jamais la même phrase 2x)
  - LLM en fallback uniquement pour les cas « flou métier » (économie de tokens)
  - Handover humain après 2 hors-sujet consécutifs
"""

import os
import re
import json
import random
import unicodedata
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from difflib import SequenceMatcher

import yaml
import requests

# ---------------------------------------------------------
#  CHARGEMENT DU YAML
# ---------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent   # repo root (api/ est à côté du yaml)
YAML_PATH = ROOT / "betty_btp_abc.yaml"

with open(YAML_PATH, "r", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

LEX          = CFG["lexique"]
RECAD        = CFG["recadrages"]
FLOW         = CFG["flow"]
CLOSING_TPL  = CFG["closing"]
MAX_OFFTOPIC = CFG["behavior"].get("max_off_topic_before_handover", 2)

# index step -> entry
FLOW_KEYS = [step["key"] for step in FLOW]

# ---------------------------------------------------------
#  CONFIG LLM (Together AI, inchangé)
# ---------------------------------------------------------
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL            = os.getenv("LLM_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")

# Sessions en mémoire (⚠️ voir note en fin de fichier sur Vercel cold-start)
sessions = {}

GREETINGS = {
    "bonjour", "bonsoir", "salut", "hello", "hi", "coucou",
    "bjr", "bsr", "yo", "bj", "hey", "hola",
}

# ---------------------------------------------------------
#  HELPERS
# ---------------------------------------------------------
def normalize(txt: str) -> str:
    txt = (txt or "").lower().strip()
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", txt)


def contains_any(txt: str, keywords) -> bool:
    n = normalize(txt)
    tokens = re.findall(r"[a-z0-9]+", n)

    for token in tokens:
        for kw in keywords:
            nkw = normalize(kw)

            # match exact
            if nkw == token:
                return True

            # match tolérant fautes (🔥 clé)
            if abs(len(token) - len(nkw)) <= 2:
                if SequenceMatcher(None, token, nkw).ratio() >= 0.84:
                    return True

    return False

def looks_like_pro_request(msg: str) -> bool:
    n = normalize(msg)

    # présence chiffre → souvent surface / volume / chantier
    if re.search(r"\d", n):
        return True

    # mots métier (avec tolérance fautes)
    if contains_any(n, LEX["metier"]):
        return True

    # patterns pro
    patterns = [
        r"devis",
        r"chantier",
        r"travaux",
        r"renov",
        r"peindre",
        r"intervention",
        r"planning",
    ]

    return any(re.search(p, n) for p in patterns)
  
def pick(variants):
    if isinstance(variants, str):
        return variants
    return random.choice(variants)


def is_greeting(msg: str) -> bool:
    n = normalize(msg).rstrip("!.,?").strip()
    return n in GREETINGS


# ---------------------------------------------------------
#  VALIDATEURS par type
# ---------------------------------------------------------
def validate_telephone(msg: str) -> bool:
    digits = re.sub(r"\D", "", msg)
    return 9 <= len(digits) <= 13


def validate_email(msg: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", msg.strip()))


def validate_surface(msg: str) -> bool:
    # Au moins un chiffre OU un mot type "pièce/salon/chambre..."
    if re.search(r"\d", msg):
        return True
    return contains_any(msg, ["piece", "salon", "chambre", "cuisine", "couloir", "bureau"])


def validate_prenom(msg: str) -> bool:
    n = normalize(msg)
    # prénom simple : 2-30 caractères lettres/tirets, pas de chiffres
    if not re.match(r"^[a-zàâçéèêëîïôûùüÿñæœ\- ]{2,30}$", n):
        return False
    if re.search(r"\d", n):
        return False
    # refuse "aaa", "zzz" etc (3+ fois le même caractère consécutif)
    if re.search(r"(.)\1{2,}", n):
        return False
    # refuse les messages qui contiennent un mot hors-sujet
    if contains_any(n, LEX["hors_sujet"]):
        return False
    return True


def validate_free_text(msg: str) -> bool:
    return len(normalize(msg)) >= 2


def validate_free_text_metier(msg: str) -> bool:
    """Étape projet : DOIT contenir un mot métier.
    Plus de fallback sur la longueur sinon on accepte n'importe quoi.
    """
    return contains_any(msg, LEX["metier"])


VALIDATORS = {
    "telephone":          validate_telephone,
    "email":              validate_email,
    "surface":            validate_surface,
    "prenom":             validate_prenom,
    "free_text":          validate_free_text,
    "free_text_metier":   validate_free_text_metier,
}


# ---------------------------------------------------------
#  CLASSIFICATION
# ---------------------------------------------------------
def classify_message(msg: str, step_idx: int) -> str:
    n = normalize(msg)

    if not n:
        return "flou"

    if is_greeting(msg):
        return "greeting"

    step      = FLOW[step_idx]
    step_key  = step["key"]
    validator = VALIDATORS.get(step.get("validate", "free_text"), validate_free_text)

    # 🔥 1. PRIORITÉ : format attendu (tel, email, etc.)
    if step_key in {"telephone", "email", "surface", "prenom"}:
        if validator(msg):
            return "pertinent"
        else:
            return "invalide"

    # 🔥 2. QUESTIONS UTILES
    if contains_any(msg, LEX["info_hors_flow"]):
        return "info_hors_flow"

    # 🔥 3. DEMANDE PRO
    if looks_like_pro_request(msg):
        return "pertinent" if step_key == "projet" else "info_hors_flow"

    # 🔥 4. TOUT LE RESTE = hors sujet
    return "hors_sujet"


# ---------------------------------------------------------
#  RECADRAGES
# ---------------------------------------------------------
def recadrage_hors_sujet(session):
    session["off_topic_count"] = session.get("off_topic_count", 0) + 1
    if session["off_topic_count"] >= MAX_OFFTOPIC:
        session["off_topic_count"] = 0
        return pick(RECAD["handover"])
    return pick(RECAD["hors_sujet"])


def recadrage_info_hors_flow(msg):
    n = normalize(msg)
    if any(k in n for k in ["horaire", "ouvert", "ferme"]):
        return RECAD["info_hors_flow"]["horaires"]
    if any(k in n for k in ["contact", "telephone", "appeler", "numero", "rappel"]):
        return RECAD["info_hors_flow"]["contact"]
    if any(k in n for k in ["adresse", "ou etes", "secteur", "zone", "intervenez"]):
        return RECAD["info_hors_flow"]["adresse"]
    return RECAD["info_hors_flow"]["generique"]


def recadrage_invalide(step_key):
    variants = RECAD["invalide"].get(step_key)
    if variants:
        return pick(variants)
    return pick(RECAD["flou"])


# ---------------------------------------------------------
#  QUESTION BUILDER (avec prénom)
# ---------------------------------------------------------
def get_question(step_idx, data):
    step = FLOW[step_idx]
    q = step["question"]
    return q.format(prenom=data.get("prenom", ""))


def get_warmth(step_idx, data):
    step = FLOW[step_idx]
    w = step.get("warmth", "") or ""
    return w.format(prenom=data.get("prenom", ""))


# ---------------------------------------------------------
#  LLM (fallback pour cas flous métier, phase post-qualif)
# ---------------------------------------------------------
def call_llm(message, data):
    if not TOGETHER_API_KEY:
        return ("Je suis là pour vous aider ! Vous pouvez aussi appeler le 02 43 75 98 18 "
                "pour échanger directement avec un conseiller.")
    try:
        ctx_lines = []
        for k in ("projet", "surface", "delai", "prenom"):
            if data.get(k):
                ctx_lines.append(f"- {k} : {data[k]}")
        ctx = "\n".join(ctx_lines) or "(aucun)"

        r = requests.post(
            "https://api.together.xyz/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {TOGETHER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Tu es Betty, assistante commerciale d'ABC Peinture Déco au Mans (Sarthe). "
                            "Ton rôle : répondre chaleureusement, en 1 à 2 phrases maximum, orienté action "
                            "(devis ou rappel au 02 43 75 98 18). Jamais robotique. Pas de jargon."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Contexte client :\n{ctx}\n\nMessage : {message}",
                    },
                ],
                "temperature": 0.6,
                "max_tokens": 150,
            },
            timeout=15,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ("Je préfère que mon équipe vous réponde directement sur ce point. "
                "Appelez-nous au 02 43 75 98 18 ou laissez-moi votre numéro, on vous rappelle.")


# ---------------------------------------------------------
#  LOGIQUE CENTRALE
# ---------------------------------------------------------
def _reset_session():
    return {"step": 0, "data": {}, "qualified": False, "off_topic_count": 0}


def handle_message(user_id, message):
    s = sessions.setdefault(user_id, _reset_session())

    # ===== RESET explicite : user dit "bonjour" / "reset" =====
    # Évite les sessions fantômes qui piègent l'utilisateur à une étape avancée.
    n = normalize(message).rstrip("!.,?").strip()
    if n in GREETINGS or n in {"reset", "recommencer", "restart"}:
        sessions[user_id] = _reset_session()
        s = sessions[user_id]
        return get_question(0, {})

    # ===== FIN DE FLOW : on est en mode libre =====
    if s["qualified"]:
        label = classify_message(message, step_idx=len(FLOW) - 1)
        if label == "hors_sujet":
            return recadrage_hors_sujet(s)
        if label == "info_hors_flow":
            return recadrage_info_hors_flow(message)
        return call_llm(message, s["data"])

    step_idx = s["step"]
    label    = classify_message(message, step_idx)

    # 1) salutation au démarrage → pose la première question (déjà géré au-dessus)
    if label == "greeting":
        return get_question(step_idx, s["data"])

    # 2) hors-sujet → recadrer sans avancer
    if label == "hors_sujet":
        return recadrage_hors_sujet(s) + "\n\n" + get_question(step_idx, s["data"])

    # 3) question hors-flow utile → répondre puis relancer l'étape
    if label == "info_hors_flow":
        return recadrage_info_hors_flow(message) + "\n\n" + get_question(step_idx, s["data"])

    # 4) format invalide (tel/email/surface/prénom) → corriger sans avancer
    if label == "invalide":
        return recadrage_invalide(FLOW[step_idx]["key"]) + "\n\n" + get_question(step_idx, s["data"])

    # 5) flou → demander reformulation sans avancer
    if label == "flou":
        return pick(RECAD["flou"]) + "\n\n" + get_question(step_idx, s["data"])

    # 6) pertinent → on enregistre et on avance
    step_key = FLOW_KEYS[step_idx]
    value    = message.strip()
    if step_key == "prenom":
        value = value.capitalize()
    if step_key == "telephone":
        value = re.sub(r"\D", "", value)   # on garde uniquement les chiffres

    s["data"][step_key] = value
    s["step"] += 1
    s["off_topic_count"] = 0

    # FIN de qualification
    if s["step"] >= len(FLOW):
        s["qualified"] = True
        return CLOSING_TPL.format(prenom=s["data"].get("prenom", "")).strip()

    warmth = get_warmth(step_idx, s["data"])
    next_q = get_question(s["step"], s["data"])
    return f"{warmth}{next_q}".strip()


# ---------------------------------------------------------
#  HANDLER HTTP (Vercel serverless)
# ---------------------------------------------------------
def _json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        _json(self, {"ok": True})

    def do_GET(self):
        _json(self, {"ok": True, "bot": CFG["identity"]["name"]})

    def do_POST(self):
        try:
            length  = int(self.headers.get("Content-Length", 0))
            body    = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            message = (payload.get("message") or "").strip()
            if not message:
                return _json(self, {"response": "Je n'ai rien reçu 🤔 Pouvez-vous réessayer ?"})

            # session_id : à remplacer par un vrai cookie/JWT en prod
            user_id = payload.get("session_id") or self.client_address[0]

            reply = handle_message(user_id, message)
            return _json(self, {"response": reply})

        except Exception as e:
            return _json(self, {
                "response": ("Petit bug de mon côté 😅 Appelez-nous au 02 43 75 98 18, "
                             "on s'en occupe tout de suite."),
                "debug": str(e),
            })


# ---------------------------------------------------------
#  ⚠️  NOTE PROD — À lire
# ---------------------------------------------------------
# Vercel serverless redémarre le process entre les requêtes → `sessions = {}`
# ne survit pas aux cold starts. Deux options :
#
#   A) Vercel KV (Redis managed) : 2 lignes à ajouter, 0 config.
#   B) Passer le state dans le client (cookie signé) et le POST.
#
# Tant que tu es en phase démo / maquette, la mémoire process suffit.
# En prod réelle, bascule sur A ou B sous 24h sinon tu perdras des leads.
