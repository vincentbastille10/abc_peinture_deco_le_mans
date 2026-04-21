"""
Betty — ABC Peinture Déco
Backend Vercel serverless — VERSION CONVERSION v2

CHANGEMENTS CLÉS vs version précédente :
  1. Flow réduit à 3 étapes (projet → prénom → téléphone) + 1 bonus facultatif
  2. EMAIL SUPPRIMÉ du flow obligatoire
  3. Short-circuit téléphone : capturé dès le 1er message où il apparaît
  4. validate_projet très permissif — on ne bloque JAMAIS sur le projet
  5. Particuliers ET pros acceptés (pas de rejet "hors cible")
  6. Validation prénom assouplie (prénoms composés, accents, pseudos)
  7. Recadrages variés tirés au hasard (jamais répétitifs)
  8. site_memory.json pour répondre aux questions hors-flow (horaires, zone, etc.)
  9. LLM (Together AI) réservé au mode libre post-qualification
  10. Sessions dict en mémoire — voir note prod sur Vercel KV en bas de fichier

Structure repo attendue :
  repo_root/
    api/main.py            ← ce fichier
    betty_btp_abc.yaml     ← YAML config
    site_memory.json       ← (optionnel) réponses aux questions hors-flow
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

LEX           = CFG.get("lexique", {})
RECAD         = CFG.get("recadrages", {})
FLOW          = CFG.get("flow", [])
CLOSING_TPL   = CFG.get("closing", "Merci {prenom}, on vous rappelle au {phone}.")
MAX_OFFTOPIC  = CFG["behavior"].get("max_off_topic_before_handover", 3)
COMPANY_PHONE = CFG["identity"].get("phone", "02 43 75 98 18")

FLOW_KEYS = [step["key"] for step in FLOW]

# ---------------------------------------------------------
#  CHARGEMENT SITE_MEMORY (optionnel)
# ---------------------------------------------------------
SITE_MEMORY_PATH = ROOT / "site_memory.json"
try:
    with open(SITE_MEMORY_PATH, "r", encoding="utf-8") as _f:
        SITE_MEM = json.load(_f)
except Exception:
    SITE_MEM = {}

SITE_INTENTS   = SITE_MEM.get("intent_map", {})
SITE_RESPONSES = SITE_MEM.get("responses", {})
SITE_FALLBACK  = SITE_RESPONSES.get("fallback", ["Je regarde ça 👍"])

# ---------------------------------------------------------
#  CONFIG LLM (Together AI) — mode libre post-qualification
# ---------------------------------------------------------
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
# On hardcode le modèle pour éviter le bug LLM_MODEL Vercel
MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"

# Sessions en mémoire (voir note prod en bas)
sessions: dict = {}

# ---------------------------------------------------------
#  CONSTANTES LEXICALES
# ---------------------------------------------------------
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
#  HELPERS TEXTE
# ---------------------------------------------------------

@app.before_request
def off():
    return "Maintenance", 503
  
def normalize(txt: str) -> str:
    """Minuscule, sans accents, sans double espaces."""
    txt = (txt or "").lower().strip()
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", txt)


def fuzzy_in(token: str, keyword: str) -> bool:
    """Match tolérant aux fautes de frappe (ratio 0.80)."""
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
    """Cherche si le texte contient au moins un mot du lexique (fuzzy)."""
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


def pick(variants) -> str:
    """Tire une variante au hasard parmi une liste (ou retourne la chaîne)."""
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
    """Extrait un numéro FR valide depuis n'importe quel texte."""
    # Nettoyage brut
    candidate = re.sub(r"[^\d+]", "", msg)
    if candidate.startswith("+33"):
        candidate = "0" + candidate[3:]
    elif candidate.startswith("0033"):
        candidate = "0" + candidate[4:]
    # Cas 9 chiffres sans le 0 initial
    if len(candidate) == 9 and candidate.isdigit():
        candidate = "0" + candidate
    # Validation 10 chiffres commençant par 0
    if len(candidate) == 10 and candidate.startswith("0") and candidate.isdigit():
        return candidate
    # Regex fallback sur le texte original
    m = PHONE_RE.search(msg)
    if m:
        return re.sub(r"\D", "", m.group(0))[:10]
    return None


def extract_prenom(msg: str):
    """Extrait un prénom probable d'un message libre."""
    # Patterns explicites ("je m'appelle X", "c'est X", etc.)
    patterns = [
        r"(?:je m['' ]?appelle|je suis|c['' ]?est|moi c['' ]?est|mon prenom est|mon prénom est)\s+([a-zàâçéèêëîïôûùüÿñæœ\-]{2,30})",
    ]
    for p in patterns:
        m = re.search(p, normalize(msg))
        if m:
            return m.group(1).capitalize()
    # Message court = probablement juste le prénom
    tokens = re.findall(r"[A-Za-zàâçéèêëîïôûùüÿñæœ\-]{2,30}", msg)
    if len(tokens) == 1 and not contains_any(tokens[0], LEX.get("metier", [])):
        return tokens[0].capitalize()
    # Prénom composé (deux tokens courts)
    if len(tokens) == 2 and all(len(t) >= 2 for t in tokens):
        # On prend les deux si ça ne ressemble pas à un projet
        combined = tokens[0].capitalize() + "-" + tokens[1].capitalize()
        if not contains_any(msg, LEX.get("metier", [])):
            return combined
        return tokens[0].capitalize()
    return None


def detect_urgent(msg: str) -> bool:
    return contains_any(msg, list(URGENT_WORDS))


def detect_devis_intent(msg: str) -> bool:
    return contains_any(msg, list(DEVIS_WORDS))


# ---------------------------------------------------------
#  VALIDATEURS — intentionnellement permissifs
# ---------------------------------------------------------
def validate_telephone(msg: str) -> bool:
    return extract_phone(msg) is not None


def validate_surface(msg: str) -> bool:
    """Toujours vrai — étape bonus, on n'est jamais bloquant."""
    return True


def validate_prenom(msg: str) -> bool:
    """Accepte les prénoms simples, composés, avec accents, pseudos courts."""
    n = normalize(msg)
    if not n:
        return False
    # Pas de chiffres dans un prénom
    if re.search(r"\d", n):
        return False
    # Pas de répétitions absurdes (aaaa, zzzz)
    if re.search(r"(.)\1{3,}", n):
        return False
    letters = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ]", n)
    return 2 <= len(letters) <= 60  # Élargi pour les prénoms composés


def validate_projet(msg: str) -> bool:
    """
    Très permissif — on ne bloque jamais sur le projet.
    On accepte n'importe quel texte de 2+ caractères non-vide.
    La qualification humaine se fait au rappel téléphonique.
    """
    n = normalize(msg)
    if len(n) < 2:
        return False
    if is_skip(msg):
        return False
    return True


def validate_free_text(msg: str) -> bool:
    return len(normalize(msg)) >= 1


VALIDATORS = {
    "telephone": validate_telephone,
    "surface":   validate_surface,
    "prenom":    validate_prenom,
    "projet":    validate_projet,
    "free_text": validate_free_text,
}


# ---------------------------------------------------------
#  DÉTECTION INTENTION HORS-FLOW (site_memory + YAML)
# ---------------------------------------------------------
def detect_site_intent(msg: str):
    """Retourne la clé d'intention (horaires, contact, zone…) ou None."""
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


def detect_info_hors_flow(msg: str) -> bool:
    """Détecte les questions légitimes qui ne sont pas une étape du flow."""
    return bool(detect_site_intent(msg) or contains_any(msg, LEX.get("info_hors_flow", [])))


# ---------------------------------------------------------
#  CLASSIFICATION DU MESSAGE
# ---------------------------------------------------------
def classify_message(msg: str, step_idx: int) -> str:
    """
    Retourne : greeting | pertinent | invalide | flou | info_hors_flow
    NB : hors_sujet n'existe plus — on ne rejette plus de leads.
    """
    n = normalize(msg)
    if not n:
        return "flou"
    if is_greeting(msg):
        return "greeting"

    step = FLOW[step_idx]
    step_key = step["key"]
    validator = VALIDATORS.get(step.get("validate", "free_text"), validate_free_text)

    # Étape téléphone : binaire (valide ou invalide)
    if step_key == "telephone":
        return "pertinent" if validator(msg) else "invalide"

    # Étape prénom : valide ou demande de reformulation douce
    if step_key == "prenom":
        return "pertinent" if validator(msg) else "invalide"

    # Étape surface : toujours accepté (bonus facultatif)
    if step_key == "surface":
        return "pertinent"

    # Étape projet : très permissive
    if step_key == "projet":
        # Question hors-flow répond AVANT de classifier comme flou
        if detect_info_hors_flow(msg):
            return "info_hors_flow"
        if validator(msg):
            return "pertinent"
        return "flou"

    # Fallback
    if detect_info_hors_flow(msg):
        return "info_hors_flow"

    return "pertinent"


# ---------------------------------------------------------
#  RECADRAGES
# ---------------------------------------------------------
def recadrage_hors_sujet(session: dict) -> str:
    session["off_topic_count"] = session.get("off_topic_count", 0) + 1
    if session["off_topic_count"] >= MAX_OFFTOPIC:
        session["off_topic_count"] = 0
        return pick(RECAD.get("handover", [f"Laissez-moi votre numéro, on vous rappelle au {COMPANY_PHONE}."]))
    return pick(RECAD.get("hors_sujet", ["Revenons à votre projet 👍"]))


def recadrage_info_hors_flow(msg: str) -> str:
    """Répond via site_memory.json d'abord, puis fallback YAML."""
    intent = detect_site_intent(msg)
    if intent and intent in SITE_RESPONSES:
        return pick(SITE_RESPONSES[intent])

    # Fallback YAML (compatibilité)
    n = normalize(msg)
    infos = RECAD.get("info_hors_flow", {})
    if any(k in n for k in ["horaire", "ouvert", "ferme", "dispo"]):
        return infos.get("horaires", pick(SITE_FALLBACK))
    if any(k in n for k in ["adresse", "secteur", "zone", "ville", "deplac"]):
        return infos.get("adresse", pick(SITE_FALLBACK))
    if any(k in n for k in ["garantie", "assurance", "decennale"]):
        return infos.get("garantie", infos.get("generique", pick(SITE_FALLBACK)))
    if any(k in n for k in ["delai", "quand", "combien de temps"]):
        return infos.get("delai", infos.get("generique", pick(SITE_FALLBACK)))
    return infos.get("generique", pick(SITE_FALLBACK))


def recadrage_invalide(step_key: str) -> str:
    variants = RECAD.get("invalide", {}).get(step_key)
    if variants:
        return pick(variants)
    return pick(RECAD.get("flou", ["Pouvez-vous préciser ?"]))


# ---------------------------------------------------------
#  QUESTION / WARMTH BUILDERS
# ---------------------------------------------------------
def get_question(step_idx: int, data: dict) -> str:
    step = FLOW[step_idx]
    return step["question"].format(prenom=data.get("prenom") or "")


def get_warmth(step_idx: int, data: dict) -> str:
    step = FLOW[step_idx]
    w = step.get("warmth") or ""
    return w.format(prenom=data.get("prenom") or "")


# ---------------------------------------------------------
#  SHORT-CIRCUIT — capture opportuniste téléphone + urgence
# ---------------------------------------------------------
def opportunistic_capture(session: dict, msg: str) -> None:
    """Si un téléphone apparaît dans n'importe quel message, on le capture."""
    data = session["data"]
    if not data.get("telephone"):
        ph = extract_phone(msg)
        if ph:
            data["telephone"] = ph
    if detect_urgent(msg):
        data["_urgent"] = True


def advance_past_captured(session: dict) -> None:
    """Avance le curseur sur toutes les étapes déjà remplies (short-circuit)."""
    while session["step"] < len(FLOW):
        key = FLOW_KEYS[session["step"]]
        if session["data"].get(key):
            session["step"] += 1
        else:
            break


# ---------------------------------------------------------
#  LLM (Together AI) — mode libre post-qualification uniquement
# ---------------------------------------------------------
def call_llm(message: str, data: dict) -> str:
    if not TOGETHER_API_KEY:
        return (f"Je transmets votre message à l'équipe. "
                f"Pour un retour immédiat, appelez le {COMPANY_PHONE}.")
    try:
        ctx_lines = []
        for k in ("projet", "surface", "prenom", "telephone"):
            if data.get(k):
                ctx_lines.append(f"- {k} : {data[k]}")
        ctx = "\n".join(ctx_lines) or "(aucun contexte)"
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
                            f"Tu réponds en 1 à 2 phrases max, ton chaleureux et direct. "
                            f"Tu orientes toujours vers un rappel rapide au {COMPANY_PHONE}. "
                            f"Jamais robotique, jamais de liste à puces."
                        ),
                    },
                    {"role": "user", "content": f"Contexte client :\n{ctx}\n\nMessage : {message}"},
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
def _reset_session() -> dict:
    return {
        "step": 0,
        "data": {},
        "qualified": False,
        "off_topic_count": 0,
        "msg_count": 0,
    }


def send_lead(data: dict) -> None:
    """Envoie le lead au webhook configuré (LEAD_WEBHOOK_URL)."""
    webhook_url = os.getenv("LEAD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    clean_data = {
        "projet":    (data.get("projet") or "").strip(),
        "surface":   (data.get("surface") or "").strip(),
        "prenom":    (data.get("prenom") or "").strip(),
        "telephone": re.sub(r"\D", "", (data.get("telephone") or "").strip()),
        "urgent":    bool(data.get("_urgent")),
        # email supprimé du flow — champ vide pour rétrocompatibilité CRM
        "email":     "",
    }
    try:
        requests.post(
            webhook_url,
            json={"source": "betty_abc_peinture", "data": clean_data},
            timeout=3,
        )
    except Exception:
        pass


# ---------------------------------------------------------
#  LOGIQUE CENTRALE
# ---------------------------------------------------------
def handle_message(user_id: str, message: str) -> str:
    n = normalize(message)

    # Reset explicite ou salutation simple → on repart au début
    is_simple_greeting = is_greeting(message) and len(n.split()) <= 2
    is_reset_cmd = n in {"reset", "recommencer", "restart", "reinit"}
    if is_reset_cmd or is_simple_greeting:
        sessions[user_id] = _reset_session()
        return get_question(0, {})

    s = sessions.setdefault(user_id, _reset_session())
    s["msg_count"] += 1

    # ── Short-circuit : capture téléphone + urgence partout ──
    opportunistic_capture(s, message)
    advance_past_captured(s)

    # ── Qualification déjà complète ? ──
    def _is_qualified(data):
        return (data.get("telephone") and data.get("prenom") and data.get("projet"))

    if _is_qualified(s["data"]) and not s["qualified"]:
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(
            prenom=s["data"].get("prenom") or "",
            phone=COMPANY_PHONE,
        ).strip()

    # ── Mode libre post-qualification ──
    if s["qualified"] or s["step"] >= len(FLOW):
        if not s["qualified"]:
            s["qualified"] = True
            send_lead(s["data"])
            return CLOSING_TPL.format(
                prenom=s["data"].get("prenom") or "",
                phone=COMPANY_PHONE,
            ).strip()
        if detect_info_hors_flow(message):
            return recadrage_info_hors_flow(message)
        return call_llm(message, s["data"])

    step_idx = s["step"]
    step_key = FLOW_KEYS[step_idx]

    # ── Réponse rapide aux questions hors-flow (horaires, zone…) ──
    # On vérifie qu'on n'est pas en train de confondre avec la réponse attendue
    is_phone_step = step_key == "telephone" and validate_telephone(message)
    is_prenom_step = step_key == "prenom" and validate_prenom(message) and len(n.split()) <= 2
    if detect_info_hors_flow(message) and not is_phone_step and not is_prenom_step:
        info = recadrage_info_hors_flow(message)
        next_q = get_question(step_idx, s["data"])
        return f"{info}\n{next_q}".strip()

    label = classify_message(message, step_idx)

    # ── Intent devis explicite à l'étape projet → on accepte sans bloquer ──
    if step_key == "projet" and detect_devis_intent(message) and not s["data"].get("projet"):
        s["data"]["projet"] = message.strip()
        s["step"] += 1
        advance_past_captured(s)
        if s["step"] >= len(FLOW):
            s["qualified"] = True
            send_lead(s["data"])
            return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
        next_q = get_question(s["step"], s["data"])
        return f"Très bien, je note. {next_q}".strip()

    if label == "greeting":
        return get_question(step_idx, s["data"])

    if label == "info_hors_flow":
        info = recadrage_info_hors_flow(message)
        next_q = get_question(step_idx, s["data"])
        return f"{info}\n{next_q}".strip()

    if label == "invalide":
        return f"{recadrage_invalide(step_key)} {get_question(step_idx, s['data'])}".strip()

    if label == "flou":
        s["off_topic_count"] = s.get("off_topic_count", 0) + 1
        if step_key == "projet":
            # Après 2 flous sur le projet, on accepte tel quel et on avance
            if s["off_topic_count"] >= 2:
                s["data"]["projet"] = message.strip() or "à préciser au rappel"
                s["step"] += 1
                s["off_topic_count"] = 0
                advance_past_captured(s)
                if s["step"] >= len(FLOW):
                    s["qualified"] = True
                    send_lead(s["data"])
                    return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
                next_q = get_question(s["step"], s["data"])
                return f"Pas de souci, on précisera au téléphone. {next_q}".strip()
        # Handover si trop de flous consécutifs
        if s["off_topic_count"] >= MAX_OFFTOPIC:
            s["off_topic_count"] = 0
            return pick(RECAD.get("handover", [f"Laissez-moi votre numéro, on vous rappelle au {COMPANY_PHONE}."]))
        return f"{pick(RECAD.get('flou', ['En quelques mots, quel est votre besoin ?']))} {get_question(step_idx, s['data'])}".strip()

    # ── Label "pertinent" → on enregistre et on avance ──
    value = message.strip()

    if step_key == "prenom":
        extracted = extract_prenom(message)
        value = extracted if extracted else value.split()[0].capitalize()

    if step_key == "telephone":
        ph = extract_phone(message)
        if ph:
            value = ph

    if step_key == "surface":
        value = "" if is_skip(message) else message.strip()

    # On enregistre (y compris surface vide = étape skippée)
    if value or step_key == "surface":
        s["data"][step_key] = value

    s["step"] += 1
    s["off_topic_count"] = 0
    advance_past_captured(s)

    # Qualification atteinte ?
    if _is_qualified(s["data"]) and not s["qualified"]:
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(
            prenom=s["data"].get("prenom") or "",
            phone=COMPANY_PHONE,
        ).strip()

    if s["step"] >= len(FLOW):
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(
            prenom=s["data"].get("prenom") or "",
            phone=COMPANY_PHONE,
        ).strip()

    warmth = get_warmth(step_idx, s["data"])
    next_q = get_question(s["step"], s["data"])
    return f"{warmth}{next_q}".strip()


# ---------------------------------------------------------
#  HANDLER HTTP (Vercel serverless)
# ---------------------------------------------------------
def _json_response(handler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence les logs HTTP verbeux

    def do_OPTIONS(self):
        _json_response(self, {"ok": True})

    def do_GET(self):
        _json_response(self, {"ok": True, "bot": CFG["identity"]["name"]})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            message = (payload.get("message") or "").strip()

            if not message:
                return _json_response(self, {
                    "response": "Je n'ai rien reçu 🤔 Pouvez-vous réessayer ?"
                })

            user_id = payload.get("session_id") or self.client_address[0]
            reply = handle_message(user_id, message)
            return _json_response(self, {"response": reply})

        except Exception as e:
            return _json_response(self, {
                "response": (
                    f"Petit bug de mon côté, désolée 😅 "
                    f"Appelez-nous directement au {COMPANY_PHONE}, on s'en occupe tout de suite."
                ),
                "debug": str(e),
            })


# ---------------------------------------------------------
#  NOTE PRODUCTION VERCEL
# ---------------------------------------------------------
# Le dict `sessions` ne survit pas aux cold-starts Vercel (serverless).
# Pour les leads à fort trafic, deux solutions :
#
#   Option A — Vercel KV (Redis managé, ~0€ pour votre volume) :
#     pip install vercel-kv
#     Remplacer sessions[user_id] = ... par kv.set(user_id, json, ex=3600)
#
#   Option B — State côté client :
#     Le front envoie session_state (JSON signé) à chaque POST
#     Le backend n'a besoin d'aucun storage persistant
#
# Pour l'instant en demo/faible trafic, le dict en mémoire fonctionne.
