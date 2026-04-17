"""
Betty — ABC Peinture Déco
Backend Vercel serverless — VERSION CONVERSION FINALE
(flow court + short-circuit téléphone + intégration site_memory.json)

Objectif unique : capturer prénom + téléphone + projet en 3 messages max.

Structure de repo attendue :
  repo_root/
    api/main.py            ← ce fichier
    betty_btp_abc.yaml
    site_memory.json
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
ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "betty_btp_abc.yaml"

with open(YAML_PATH, "r", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

LEX          = CFG["lexique"]
RECAD        = CFG["recadrages"]
FLOW         = CFG["flow"]
CLOSING_TPL  = CFG["closing"]
MAX_OFFTOPIC = CFG["behavior"].get("max_off_topic_before_handover", 3)
COMPANY_PHONE = CFG["identity"].get("phone", "02 43 75 98 18")

FLOW_KEYS = [step["key"] for step in FLOW]

# ---------------------------------------------------------
#  CHARGEMENT DE LA MÉMOIRE SITE (site_memory.json)
# ---------------------------------------------------------
SITE_MEMORY_PATH = ROOT / "site_memory.json"
try:
    with open(SITE_MEMORY_PATH, "r", encoding="utf-8") as _f:
        SITE_MEM = json.load(_f)
except Exception:
    SITE_MEM = {}

SITE_INTENTS    = SITE_MEM.get("intent_map", {})
SITE_RESPONSES  = SITE_MEM.get("responses", {})
SITE_FALLBACK   = SITE_RESPONSES.get("fallback", ["Je regarde ça 👍"])
SITE_CONVERSION = SITE_MEM.get("conversion", {})

# ---------------------------------------------------------
#  CONFIG LLM (Together AI) — fallback uniquement
# ---------------------------------------------------------
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL            = os.getenv("LLM_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")

# Sessions en mémoire
sessions = {}

GREETINGS = {
    "bonjour", "bonsoir", "salut", "hello", "hi", "coucou",
    "bjr", "bsr", "yo", "bj", "hey", "hola", "slt", "cc",
    "bonjours", "salu", "salutation", "salutations",
}

SKIP_WORDS = {
    "non", "rien", "skip", "passe", "passer", "plus tard",
    "je sais pas", "sais pas", "aucune idee", "aucune idée",
    "pas encore", "pas sur", "pas sûr", "nsp", "je ne sais pas",
    "peu importe", "ne sais pas",
}

URGENT_WORDS = {
    "urgent", "urgence", "vite", "rapide", "rapidement",
    "au plus vite", "asap", "immediat", "immédiat",
    "des que possible", "dès que possible", "tout de suite",
    "aujourd hui", "aujourdhui", "demain",
}

DEVIS_WORDS = {
    "devis", "tarif", "prix", "estimation", "cout", "coût",
    "combien", "budget", "chiffrage", "rappel", "rappeler",
    "me rappeler", "rappelez", "contact", "contacter",
}

# ---------------------------------------------------------
#  HELPERS
# ---------------------------------------------------------
def normalize(txt: str) -> str:
    txt = (txt or "").lower().strip()
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", txt)


def fuzzy_in(token: str, keyword: str) -> bool:
    """Match tolérant aux fautes de frappe."""
    token = normalize(token)
    keyword = normalize(keyword)
    if not token or not keyword:
        return False
    if token == keyword:
        return True
    if keyword in token or token in keyword:
        if min(len(token), len(keyword)) >= 4:
            return True
    if abs(len(token) - len(keyword)) <= 2:
        ratio = SequenceMatcher(None, token, keyword).ratio()
        if ratio >= 0.80:
            return True
    return False


def contains_any(txt: str, keywords) -> bool:
    n = normalize(txt)
    tokens = re.findall(r"[a-z0-9']+", n)
    for token in tokens:
        for kw in keywords:
            if fuzzy_in(token, kw):
                return True
    joined = " " + n + " "
    for kw in keywords:
        nkw = " " + normalize(kw) + " "
        if " " in nkw.strip() and nkw in joined:
            return True
    return False


def pick(variants):
    if isinstance(variants, str):
        return variants
    if not variants:
        return ""
    return random.choice(variants)


def is_greeting(msg: str) -> bool:
    n = normalize(msg).rstrip("!.,?").strip()
    return n in GREETINGS


def is_skip(msg: str) -> bool:
    n = normalize(msg).rstrip("!.,?").strip()
    if n in SKIP_WORDS:
        return True
    for w in SKIP_WORDS:
        if n == w:
            return True
    return False


# ---------------------------------------------------------
#  EXTRACTEURS (short-circuit)
# ---------------------------------------------------------
PHONE_RE = re.compile(
    r"(?:(?:\+33|0033|0)\s*[1-9](?:[\s.\-]*\d){8})"
)

def extract_phone(msg: str):
    """Extrait un numéro FR depuis n'importe quel texte."""
    candidate = re.sub(r"[^\d+]", "", msg)
    if candidate.startswith("+33"):
        candidate = "0" + candidate[3:]
    elif candidate.startswith("0033"):
        candidate = "0" + candidate[4:]
    if 9 <= len(candidate) <= 11 and candidate.isdigit():
        if len(candidate) == 9 and not candidate.startswith("0"):
            candidate = "0" + candidate
        if len(candidate) == 10 and candidate.startswith("0"):
            return candidate
        if 9 <= len(candidate) <= 11:
            return candidate
    m = PHONE_RE.search(msg)
    if m:
        return re.sub(r"\D", "", m.group(0))[:10]
    return None


def extract_prenom(msg: str):
    """Extrait un prénom probable d'un message libre."""
    patterns = [
        r"(?:je m['’ ]?appelle|je suis|c['’ ]?est|moi c['’ ]?est|mon prenom est|mon prénom est)\s+([a-zàâçéèêëîïôûùüÿñæœ\-]{2,30})",
    ]
    n = msg.strip()
    for p in patterns:
        m = re.search(p, normalize(n))
        if m:
            return m.group(1).capitalize()
    tokens = re.findall(r"[A-Za-zàâçéèêëîïôûùüÿñæœ\-]{2,30}", n)
    if len(tokens) == 1 and not contains_any(tokens[0], LEX.get("metier", [])):
        return tokens[0].capitalize()
    if len(tokens) == 2 and all(len(t) >= 2 for t in tokens):
        return tokens[0].capitalize()
    return None


def detect_urgent(msg: str) -> bool:
    return contains_any(msg, list(URGENT_WORDS))


def detect_devis_intent(msg: str) -> bool:
    return contains_any(msg, list(DEVIS_WORDS))


# ---------------------------------------------------------
#  VALIDATEURS (permissifs mais exploitables)
# ---------------------------------------------------------
def validate_telephone(msg: str) -> bool:
    return extract_phone(msg) is not None


def validate_surface(msg: str) -> bool:
    if is_skip(msg):
        return True
    return len(normalize(msg)) >= 1


def validate_prenom(msg: str) -> bool:
    n = normalize(msg)
    if not n:
        return False
    if re.search(r"\d", n):
        return False
    if re.search(r"(.)\1{3,}", n):
        return False
    letters = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ]", n)
    return len(letters) >= 2 and len(n) <= 40


def validate_free_text(msg: str) -> bool:
    return len(normalize(msg)) >= 1


def validate_projet(msg: str) -> bool:
    """Très permissif : on ne bloque jamais sur le projet."""
    n = normalize(msg)
    if len(n) < 2:
        return False
    if is_skip(msg):
        return False
    return True


VALIDATORS = {
    "telephone":  validate_telephone,
    "surface":    validate_surface,
    "prenom":     validate_prenom,
    "free_text":  validate_free_text,
    "projet":     validate_projet,
}


# ---------------------------------------------------------
#  DÉTECTION INTENTION SITE_MEMORY
# ---------------------------------------------------------
def detect_site_intent(msg: str):
    """Retourne la clé d'intention (horaires, contact, zone, services) ou None."""
    if not SITE_INTENTS:
        return None
    n = normalize(msg)
    tokens = set(re.findall(r"[a-z0-9']+", n))
    best = None
    for intent, keywords in SITE_INTENTS.items():
        for kw in keywords:
            nkw = normalize(kw)
            if nkw in tokens:
                return intent
            for tok in tokens:
                if fuzzy_in(tok, nkw):
                    best = intent
    return best


# ---------------------------------------------------------
#  CLASSIFICATION
# ---------------------------------------------------------
def classify_message(msg: str, step_idx: int) -> str:
    n = normalize(msg)
    if not n:
        return "flou"
    if is_greeting(msg):
        return "greeting"

    step = FLOW[step_idx]
    step_key = step["key"]
    validator = VALIDATORS.get(step.get("validate", "free_text"), validate_free_text)

    if step_key == "telephone":
        if validator(msg):
            return "pertinent"
        return "invalide"

    if step_key == "prenom":
        if validator(msg):
            return "pertinent"
        return "invalide"

    if step_key == "surface":
        return "pertinent"

    # Étape projet : permissif
    if step_key == "projet":
        if validator(msg):
            return "pertinent"
        return "flou"

    # Questions hors flow (horaires, adresse, etc.) — détectées via site_memory
    if detect_site_intent(msg) or contains_any(msg, LEX.get("info_hors_flow", [])):
        return "info_hors_flow"

    return "pertinent"


# ---------------------------------------------------------
#  RECADRAGES
# ---------------------------------------------------------
def recadrage_hors_sujet(session):
    session["off_topic_count"] = session.get("off_topic_count", 0) + 1
    if session["off_topic_count"] >= MAX_OFFTOPIC:
        session["off_topic_count"] = 0
        return pick(RECAD.get("handover", ["Laissez-moi votre numéro, un artisan vous rappelle."]))
    return pick(RECAD.get("hors_sujet", ["Revenons à votre projet 👍"]))


def recadrage_info_hors_flow(msg):
    """Répond via site_memory.json. Fallback : anciennes variantes du YAML."""
    intent = detect_site_intent(msg)
    if intent and intent in SITE_RESPONSES:
        return pick(SITE_RESPONSES[intent])

    # Fallback YAML (compatibilité descendante)
    n = normalize(msg)
    infos = RECAD.get("info_hors_flow", {})
    if any(k in n for k in ["horaire", "ouvert", "ferme", "dispo"]):
        return infos.get("horaires", pick(SITE_FALLBACK))
    if any(k in n for k in ["adresse", "ou etes", "secteur", "zone", "ville", "deplac"]):
        return infos.get("adresse", pick(SITE_FALLBACK))
    if any(k in n for k in ["garantie", "assurance", "decennale"]):
        return infos.get("garantie", infos.get("generique", pick(SITE_FALLBACK)))
    if any(k in n for k in ["delai", "quand", "combien de temps"]):
        return infos.get("delai", infos.get("generique", pick(SITE_FALLBACK)))
    return infos.get("generique", pick(SITE_FALLBACK))


def recadrage_invalide(step_key):
    variants = RECAD.get("invalide", {}).get(step_key)
    if variants:
        return pick(variants)
    return pick(RECAD.get("flou", ["Pouvez-vous préciser ?"]))


# ---------------------------------------------------------
#  QUESTION BUILDER
# ---------------------------------------------------------
def get_question(step_idx, data):
    step = FLOW[step_idx]
    q = step["question"]
    return q.format(prenom=data.get("prenom", "") or "")


def get_warmth(step_idx, data):
    step = FLOW[step_idx]
    w = step.get("warmth", "") or ""
    return w.format(prenom=data.get("prenom", "") or "")


# ---------------------------------------------------------
#  SHORT-CIRCUIT
# ---------------------------------------------------------
def opportunistic_capture(session, msg):
    """Capte téléphone + signal d'urgence dès qu'ils apparaissent."""
    data = session["data"]
    if not data.get("telephone"):
        ph = extract_phone(msg)
        if ph:
            data["telephone"] = ph
    if detect_urgent(msg):
        data["_urgent"] = True


def advance_past_captured(session):
    """Avance le curseur sur toutes les étapes déjà remplies."""
    while session["step"] < len(FLOW):
        key = FLOW_KEYS[session["step"]]
        if session["data"].get(key):
            session["step"] += 1
        else:
            break


# ---------------------------------------------------------
#  LLM (fallback post-qualification uniquement)
# ---------------------------------------------------------
def call_llm(message, data):
    if not TOGETHER_API_KEY:
        return (f"Je transmets votre message à l'équipe. "
                f"Pour un retour immédiat, appelez le {COMPANY_PHONE}.")
    try:
        ctx_lines = []
        for k in ("projet", "surface", "delai", "prenom", "telephone"):
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
                            f"Tu es Betty, assistante d'ABC Peinture Déco (Le Mans). "
                            f"Tu réponds en 1 à 2 phrases max, chaleureux, direct. "
                            f"Toujours orienté rappel rapide au {COMPANY_PHONE}. "
                            f"Jamais robotique."
                        ),
                    },
                    {"role": "user", "content": f"Contexte :\n{ctx}\n\nMessage : {message}"},
                ],
                "temperature": 0.6,
                "max_tokens": 120,
            },
            timeout=10,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return (f"Laissez-moi votre numéro, un artisan vous rappelle dans la journée. "
                f"Ou appelez directement le {COMPANY_PHONE}.")


# ---------------------------------------------------------
#  SESSION / LEAD
# ---------------------------------------------------------
def _reset_session():
    return {
        "step": 0,
        "data": {},
        "qualified": False,
        "off_topic_count": 0,
        "msg_count": 0,
    }


def send_lead(data):
    webhook_url = os.getenv("LEAD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    clean_data = {
        "projet":    (data.get("projet") or "").strip(),
        "surface":   (data.get("surface") or "").strip(),
        "delai":     (data.get("delai") or "").strip(),
        "prenom":    (data.get("prenom") or "").strip(),
        "telephone": re.sub(r"\D", "", (data.get("telephone") or "").strip()),
        "urgent":    bool(data.get("_urgent")),
    }
    try:
        requests.post(
            webhook_url,
            json={"source": "betty", "data": clean_data},
            timeout=3,
        )
    except Exception:
        return


# ---------------------------------------------------------
#  LOGIQUE CENTRALE
# ---------------------------------------------------------
def handle_message(user_id, message):
    n = normalize(message)

    # Reset explicite
    if n in {"reset", "recommencer", "restart", "reinit"} or (
        is_greeting(message) and len(n.split()) <= 2
    ):
        sessions[user_id] = _reset_session()
        return get_question(0, {})

    s = sessions.setdefault(user_id, _reset_session())
    s["msg_count"] += 1

    # 🔥 Short-circuit capture (tel + urgence)
    opportunistic_capture(s, message)
    advance_past_captured(s)

    # 🔥 Short-circuit site_memory : question horaires / contact / zone / services
    # → on répond IMMÉDIATEMENT puis on relance le flow
    if not s["qualified"] and detect_site_intent(message) and s["step"] < len(FLOW):
        # on vérifie qu'on n'est pas sur une étape où le message EST la réponse
        # attendue (ex: l'utilisateur donne son téléphone, qui matcherait "contact")
        current_step = FLOW_KEYS[s["step"]] if s["step"] < len(FLOW) else None
        msg_is_answer = False
        if current_step == "telephone" and validate_telephone(message):
            msg_is_answer = True
        if current_step == "prenom" and validate_prenom(message) and len(n.split()) <= 2:
            msg_is_answer = True

        if not msg_is_answer:
            info = recadrage_info_hors_flow(message)
            next_q = get_question(s["step"], s["data"])
            return f"{info}\n{next_q}".strip()

    # Qualification déjà complète ?
    if (s["data"].get("telephone")
            and s["data"].get("prenom")
            and s["data"].get("projet")
            and not s["qualified"]):
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(
            prenom=s["data"].get("prenom", "") or "",
            phone=COMPANY_PHONE,
        ).strip()

    # Mode libre après qualification
    if s["qualified"] or s["step"] >= len(FLOW):
        if not s["qualified"]:
            s["qualified"] = True
            send_lead(s["data"])
            return CLOSING_TPL.format(
                prenom=s["data"].get("prenom", "") or "",
                phone=COMPANY_PHONE,
            ).strip()
        if detect_site_intent(message):
            return recadrage_info_hors_flow(message)
        label = classify_message(message, step_idx=len(FLOW) - 1)
        if label == "info_hors_flow":
            return recadrage_info_hors_flow(message)
        return call_llm(message, s["data"])

    step_idx = s["step"]
    step_key = FLOW_KEYS[step_idx]
    label    = classify_message(message, step_idx)

    # Intention devis explicite en étape projet
    if step_key == "projet" and detect_devis_intent(message) and not s["data"].get("projet"):
        s["data"]["projet"] = message.strip()
        s["step"] += 1
        advance_past_captured(s)
        next_q = get_question(s["step"], s["data"]) if s["step"] < len(FLOW) else ""
        return f"Très bien, je prépare ça tout de suite. {next_q}".strip()

    if label == "greeting":
        return get_question(step_idx, s["data"])

    # Info hors flow : on répond via site_memory puis on relance
    if label == "info_hors_flow":
        info = recadrage_info_hors_flow(message)
        next_q = get_question(step_idx, s["data"])
        return f"{info}\n{next_q}".strip()

    if label == "invalide":
        return f"{recadrage_invalide(step_key)} {get_question(step_idx, s['data'])}".strip()

    if label == "flou":
        s["off_topic_count"] = s.get("off_topic_count", 0) + 1
        if step_key == "projet" and s["off_topic_count"] >= 2:
            s["data"]["projet"] = message.strip() or "à préciser au rappel"
            s["step"] += 1
            s["off_topic_count"] = 0
            advance_past_captured(s)
            next_q = get_question(s["step"], s["data"]) if s["step"] < len(FLOW) else ""
            return f"Pas de souci, on précisera au téléphone. {next_q}".strip()
        return f"{pick(RECAD.get('flou', []))} {get_question(step_idx, s['data'])}".strip()

    # Pertinent → enregistrement + avance
    value = message.strip()

    if step_key == "prenom":
        extracted = extract_prenom(message)
        if extracted:
            value = extracted
        else:
            value = value.split()[0].capitalize()

    if step_key == "telephone":
        ph = extract_phone(message)
        if ph:
            value = ph

    if step_key == "surface":
        if is_skip(message):
            value = ""
        else:
            value = message.strip()

    if value or step_key == "surface":
        s["data"][step_key] = value

    s["step"] += 1
    s["off_topic_count"] = 0
    advance_past_captured(s)

    if s["step"] >= len(FLOW):
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(
            prenom=s["data"].get("prenom", "") or "",
            phone=COMPANY_PHONE,
        ).strip()

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

            user_id = payload.get("session_id") or self.client_address[0]
            reply = handle_message(user_id, message)
            return _json(self, {"response": reply})

        except Exception as e:
            return _json(self, {
                "response": (f"Petit bug de mon côté. Appelez-nous au {COMPANY_PHONE}, "
                             f"on s'en occupe tout de suite."),
                "debug": str(e),
            })


# ---------------------------------------------------------
#  NOTE PROD
# ---------------------------------------------------------
# Sur Vercel, le dict `sessions` ne survit pas aux cold-starts.
# Pour ne PAS perdre de leads sur la partie qualif :
#   → Vercel KV (Redis managed), ou
#   → state côté client (cookie signé renvoyé à chaque POST).
