"""
Betty — ABC Peinture Déco
Backend Vercel serverless — VERSION CONVERSION (refonte agressive)

Objectif unique : capturer prénom + téléphone + projet en 3 messages max.

Changements majeurs vs version précédente :
  - Flow raccourci : projet → prénom → téléphone → (bonus surface/délai)
  - Email SUPPRIMÉ du flow obligatoire
  - Short-circuit : si un numéro de téléphone est détecté à n'importe quel
    moment du flow, on le capture et on saute directement à l'étape suivante
    (ou on clôture si c'était la dernière brique manquante)
  - Classification plus tolérante : on n'enferme plus le user dans "hors_sujet"
    dès qu'il sort du lexique. On accepte la réponse et on qualifie plus tard.
  - Recadrages doux, variés, orientés "rappel rapide"
  - Particuliers acceptés (le bot ne filtre plus)
  - Détection d'intention "devis rapide" → raccourci téléphone immédiat
  - Tolérance fautes renforcée (seuil 0.80 + variantes clavier)
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
    """Match tolérant aux fautes de frappe (AZERTY / doigts gros)."""
    token = normalize(token)
    keyword = normalize(keyword)
    if not token or not keyword:
        return False
    if token == keyword:
        return True
    if keyword in token or token in keyword:
        # substring tolérée seulement si le mot court a >= 4 lettres
        if min(len(token), len(keyword)) >= 4:
            return True
    if abs(len(token) - len(keyword)) <= 2:
        ratio = SequenceMatcher(None, token, keyword).ratio()
        # 🔥 seuil abaissé à 0.80 → plus tolérant
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
    # recherche aussi en bigrammes ("appel offre", "des que possible"...)
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
    if n in GREETINGS:
        return True
    # "bonjour je voudrais un devis" → greeting + payload, pas un reset
    return False


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
    # simplifie : cherche 9-13 chiffres consécutifs (avec séparateurs)
    candidate = re.sub(r"[^\d+]", "", msg)
    # retire indicatif international
    if candidate.startswith("+33"):
        candidate = "0" + candidate[3:]
    elif candidate.startswith("0033"):
        candidate = "0" + candidate[4:]
    if 9 <= len(candidate) <= 11 and candidate.isdigit():
        # normalise sur 10 chiffres si possible
        if len(candidate) == 9 and not candidate.startswith("0"):
            candidate = "0" + candidate
        if len(candidate) == 10 and candidate.startswith("0"):
            return candidate
        if 9 <= len(candidate) <= 11:
            return candidate
    # fallback regex
    m = PHONE_RE.search(msg)
    if m:
        return re.sub(r"\D", "", m.group(0))[:10]
    return None


def extract_prenom(msg: str):
    """Extrait un prénom probable d'un message libre."""
    # patterns type "je m'appelle X", "c'est X", "moi c'est X"
    patterns = [
        r"(?:je m['’ ]?appelle|je suis|c['’ ]?est|moi c['’ ]?est|mon prenom est|mon prénom est)\s+([a-zàâçéèêëîïôûùüÿñæœ\-]{2,30})",
    ]
    n = msg.strip()
    for p in patterns:
        m = re.search(p, normalize(n))
        if m:
            return m.group(1).capitalize()
    # message d'un seul mot → probablement un prénom
    tokens = re.findall(r"[A-Za-zàâçéèêëîïôûùüÿñæœ\-]{2,30}", n)
    if len(tokens) == 1 and not contains_any(tokens[0], LEX.get("metier", [])):
        return tokens[0].capitalize()
    if len(tokens) == 2 and all(len(t) >= 2 for t in tokens):
        # "Jean Dupont" → on garde le premier
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
    # toujours OK si on répond quelque chose d'utile, sinon skip
    if is_skip(msg):
        return True
    return len(normalize(msg)) >= 1


def validate_prenom(msg: str) -> bool:
    n = normalize(msg)
    if not n:
        return False
    # accepte "Jean", "Jean-Pierre", "jp", "Mme Durand"
    if re.search(r"\d", n):
        return False
    if re.search(r"(.)\1{3,}", n):  # refuse "aaaaaa"
        return False
    # au moins 2 lettres alphabétiques
    letters = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ]", n)
    return len(letters) >= 2 and len(n) <= 40


def validate_free_text(msg: str) -> bool:
    return len(normalize(msg)) >= 1


def validate_projet(msg: str) -> bool:
    """🔥 TRÈS permissif : on ne bloque JAMAIS sur le projet.
    Tant qu'il y a du texte, on accepte. La qualification se fait humainement
    au rappel. Mieux vaut un lead "flou" qu'un utilisateur qui ferme l'onglet.
    """
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
#  CLASSIFICATION (plus tolérante, orientée conversion)
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

    # 🔥 PRIORITÉ ABSOLUE : format attendu
    if step_key == "telephone":
        # téléphone : on ne valide QUE si on extrait un vrai numéro
        if validator(msg):
            return "pertinent"
        return "invalide"

    if step_key == "prenom":
        if validator(msg):
            return "pertinent"
        return "invalide"

    if step_key == "surface":
        # surface : tout est accepté (y compris skip / texte libre)
        return "pertinent"

    # Pour l'étape projet : TRÈS permissif
    if step_key == "projet":
        if validator(msg):
            return "pertinent"
        return "flou"

    # info hors flow (horaires, adresse, etc.)
    if contains_any(msg, LEX.get("info_hors_flow", [])):
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
    n = normalize(msg)
    infos = RECAD.get("info_hors_flow", {})
    if any(k in n for k in ["horaire", "ouvert", "ferme", "dispo"]):
        return infos.get("horaires", "")
    if any(k in n for k in ["adresse", "ou etes", "secteur", "zone", "intervenez", "ville", "deplac"]):
        return infos.get("adresse", "")
    if any(k in n for k in ["garantie", "assurance", "decennale"]):
        return infos.get("garantie", infos.get("generique", ""))
    if any(k in n for k in ["delai", "quand", "combien de temps"]):
        return infos.get("delai", infos.get("generique", ""))
    return infos.get("generique", "")


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
#  SHORT-CIRCUIT : capter tout ce qu'on peut à chaque message
# ---------------------------------------------------------
def opportunistic_capture(session, msg):
    """Avant traitement, on essaie de capter un téléphone/prénom même si
    l'étape courante n'est pas là-dessus. L'utilisateur motivé qui lâche son
    06 au 1er message ne doit PAS avoir à le redonner à l'étape 3."""
    data = session["data"]

    # téléphone
    if not data.get("telephone"):
        ph = extract_phone(msg)
        if ph:
            data["telephone"] = ph

    # marqueur urgence (pour prioriser le rappel)
    if detect_urgent(msg):
        data["_urgent"] = True


def advance_past_captured(session):
    """Fait avancer le curseur de flow sur toutes les étapes dont la donnée
    est déjà capturée (short-circuit)."""
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

    # Reset explicite (bonjour seul, reset, restart)
    if n in {"reset", "recommencer", "restart", "reinit"} or (
        is_greeting(message) and len(n.split()) <= 2
    ):
        sessions[user_id] = _reset_session()
        return get_question(0, {})

    s = sessions.setdefault(user_id, _reset_session())
    s["msg_count"] += 1

    # 🔥 Short-circuit : on capte tout ce qu'on peut
    opportunistic_capture(s, message)
    advance_past_captured(s)

    # Si on a téléphone + prénom + projet → on clôt direct
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

    # FIN DE FLOW → mode libre
    if s["qualified"] or s["step"] >= len(FLOW):
        if not s["qualified"]:
            s["qualified"] = True
            send_lead(s["data"])
            return CLOSING_TPL.format(
                prenom=s["data"].get("prenom", "") or "",
                phone=COMPANY_PHONE,
            ).strip()
        # déjà qualifié : mode conversation libre
        label = classify_message(message, step_idx=len(FLOW) - 1)
        if label == "info_hors_flow":
            return recadrage_info_hors_flow(message)
        return call_llm(message, s["data"])

    step_idx = s["step"]
    step_key = FLOW_KEYS[step_idx]
    label    = classify_message(message, step_idx)

    # 🔥 intention devis explicite en étape projet → on capture + on pousse tel
    if step_key == "projet" and detect_devis_intent(message) and not s["data"].get("projet"):
        # on accepte le message comme projet (même vague, ex: "je veux un devis")
        s["data"]["projet"] = message.strip()
        s["step"] += 1
        advance_past_captured(s)
        next_q = get_question(s["step"], s["data"]) if s["step"] < len(FLOW) else ""
        return f"Très bien, je prépare ça tout de suite. {next_q}".strip()

    # greeting en cours de flow → on relance la question courante
    if label == "greeting":
        return get_question(step_idx, s["data"])

    # info hors flow → on répond brièvement puis on relance la question
    if label == "info_hors_flow":
        info = recadrage_info_hors_flow(message)
        return f"{info} {get_question(step_idx, s['data'])}".strip()

    # invalide → on redemande avec un recadrage doux
    if label == "invalide":
        return f"{recadrage_invalide(step_key)} {get_question(step_idx, s['data'])}".strip()

    # flou → recadrage ultra léger, on tente quand même de pousser
    if label == "flou":
        # après 2 flous d'affilée sur le projet, on skippe et on demande le prénom
        s["off_topic_count"] = s.get("off_topic_count", 0) + 1
        if step_key == "projet" and s["off_topic_count"] >= 2:
            s["data"]["projet"] = message.strip() or "à préciser au rappel"
            s["step"] += 1
            s["off_topic_count"] = 0
            advance_past_captured(s)
            next_q = get_question(s["step"], s["data"]) if s["step"] < len(FLOW) else ""
            return f"Pas de souci, on précisera au téléphone. {next_q}".strip()
        return f"{pick(RECAD.get('flou', []))} {get_question(step_idx, s['data'])}".strip()

    # pertinent → on enregistre et on avance
    value = message.strip()

    if step_key == "prenom":
        # tente d'extraire un prénom propre depuis un texte libre
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
            value = ""  # bonus skippé, c'est OK
        else:
            value = message.strip()

    if value or step_key == "surface":  # surface peut être vide (skip)
        s["data"][step_key] = value

    s["step"] += 1
    s["off_topic_count"] = 0
    advance_past_captured(s)

    # Fin de qualification
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
# À faire avant tout passage en prod trafic réel.
