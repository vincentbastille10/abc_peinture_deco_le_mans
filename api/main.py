"""
Betty — ABC Peinture Déco
Backend Vercel serverless — VERSION CONVERSION v2
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

ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "betty_btp_abc.yaml"

with open(YAML_PATH, "r", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

LEX = CFG.get("lexique", {})
RECAD = CFG.get("recadrages", {})
FLOW = CFG.get("flow", [])
CLOSING_TPL = CFG.get("closing", "Merci {prenom}, on vous rappelle au {phone}.")
MAX_OFFTOPIC = CFG["behavior"].get("max_off_topic_before_handover", 3)
COMPANY_PHONE = CFG["identity"].get("phone", "02 43 75 98 18")
FLOW_KEYS = [step["key"] for step in FLOW]

SITE_MEMORY_PATH = ROOT / "site_memory.json"
try:
    with open(SITE_MEMORY_PATH, "r", encoding="utf-8") as _f:
        SITE_MEM = json.load(_f)
except Exception:
    SITE_MEM = {}

SITE_INTENTS = SITE_MEM.get("intent_map", {})
SITE_RESPONSES = SITE_MEM.get("responses", {})
SITE_FALLBACK = SITE_RESPONSES.get("fallback", ["Je regarde ça 👍"])

TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
DEBUG_BETTY = str(os.getenv("DEBUG_BETTY", "0")).lower() in {"1", "true", "yes", "on"}

sessions: dict = {}

GREETINGS = {"bonjour", "bonsoir", "salut", "hello", "hi", "coucou", "bjr", "bsr", "yo", "bj", "hey", "hola", "slt", "cc", "bonjours", "salu", "salutation", "salutations"}
SKIP_WORDS = {"non", "rien", "skip", "passe", "passer", "plus tard", "je sais pas", "sais pas", "aucune idee", "aucune idée", "pas encore", "pas sur", "pas sûr", "nsp", "je ne sais pas", "peu importe", "ne sais pas"}
URGENT_WORDS = {"urgent", "urgence", "vite", "rapide", "rapidement", "au plus vite", "asap", "immediat", "immédiat", "des que possible", "dès que possible", "tout de suite", "aujourd hui", "aujourdhui", "demain"}
DEVIS_WORDS = {"devis", "tarif", "prix", "estimation", "cout", "coût", "combien", "budget", "chiffrage", "rappel", "rappeler", "me rappeler", "rappelez", "contact", "contacter"}


def normalize(txt: str) -> str:
    txt = str(txt or "").lower().strip()
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", txt)


def fuzzy_in(token: str, keyword: str) -> bool:
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
        return SequenceMatcher(None, token, keyword).ratio() >= 0.80
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


def pick(variants) -> str:
    if isinstance(variants, str):
        return variants
    if not variants:
        return ""
    return random.choice(variants)


def is_greeting(msg: str) -> bool:
    return normalize(msg).rstrip("!.,?").strip() in GREETINGS


def is_skip(msg: str) -> bool:
    return normalize(msg).rstrip("!.,?").strip() in SKIP_WORDS


PHONE_RE = re.compile(r"(?:(?:\+33|0033|0)\s*[1-9](?:[\s.\-]*\d){8})")


def extract_phone(msg: str):
    msg = str(msg or "")
    candidate = re.sub(r"[^\d+]", "", msg)
    if candidate.startswith("+33"):
        candidate = "0" + candidate[3:]
    elif candidate.startswith("0033"):
        candidate = "0" + candidate[4:]
    if len(candidate) == 9 and candidate.isdigit():
        candidate = "0" + candidate
    if len(candidate) == 10 and candidate.startswith("0") and candidate.isdigit():
        return candidate
    m = PHONE_RE.search(msg)
    if m:
        return re.sub(r"\D", "", m.group(0))[:10]
    return None


def extract_prenom(msg: str):
    msg = str(msg or "")
    patterns = [r"(?:je m['' ]?appelle|je suis|c['' ]?est|moi c['' ]?est|mon prenom est|mon prénom est)\s+([a-zàâçéèêëîïôûùüÿñæœ\-]{2,30})"]
    for p in patterns:
        m = re.search(p, normalize(msg))
        if m:
            return m.group(1).capitalize()
    tokens = re.findall(r"[A-Za-zàâçéèêëîïôûùüÿñæœ\-]{2,30}", msg)
    if len(tokens) == 1 and not contains_any(tokens[0], LEX.get("metier", [])):
        return tokens[0].capitalize()
    if len(tokens) == 2 and all(len(t) >= 2 for t in tokens):
        combined = tokens[0].capitalize() + "-" + tokens[1].capitalize()
        if not contains_any(msg, LEX.get("metier", [])):
            return combined
        return tokens[0].capitalize()
    return None


def detect_urgent(msg: str) -> bool:
    return contains_any(msg, list(URGENT_WORDS))


def detect_devis_intent(msg: str) -> bool:
    return contains_any(msg, list(DEVIS_WORDS))


def validate_telephone(msg: str) -> bool:
    return extract_phone(msg) is not None


def validate_surface(msg: str) -> bool:
    return True


def validate_prenom(msg: str) -> bool:
    n = normalize(msg)
    if not n or re.search(r"\d", n) or re.search(r"(.)\1{3,}", n):
        return False
    letters = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ]", n)
    return 2 <= len(letters) <= 60


def validate_projet(msg: str) -> bool:
    n = normalize(msg)
    return len(n) >= 2 and not is_skip(msg)


def validate_free_text(msg: str) -> bool:
    return len(normalize(msg)) >= 1


VALIDATORS = {"telephone": validate_telephone, "surface": validate_surface, "prenom": validate_prenom, "projet": validate_projet, "free_text": validate_free_text}


def detect_site_intent(msg: str):
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
    return bool(detect_site_intent(msg) or contains_any(msg, LEX.get("info_hors_flow", [])))


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
        return "pertinent" if validator(msg) else "invalide"
    if step_key == "prenom":
        return "pertinent" if validator(msg) else "invalide"
    if step_key == "surface":
        return "pertinent"
    if step_key == "projet":
        if detect_info_hors_flow(msg):
            return "info_hors_flow"
        return "pertinent" if validator(msg) else "flou"
    if detect_info_hors_flow(msg):
        return "info_hors_flow"
    return "pertinent"


def recadrage_info_hors_flow(msg: str) -> str:
    intent = detect_site_intent(msg)
    if intent and intent in SITE_RESPONSES:
        return pick(SITE_RESPONSES[intent])
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


def get_question(step_idx: int, data: dict) -> str:
    step = FLOW[step_idx]
    return step["question"].format(prenom=data.get("prenom") or "")


def get_warmth(step_idx: int, data: dict) -> str:
    step = FLOW[step_idx]
    return (step.get("warmth") or "").format(prenom=data.get("prenom") or "")


def opportunistic_capture(session: dict, msg: str) -> None:
    data = session["data"]
    if not data.get("telephone"):
        ph = extract_phone(msg)
        if ph:
            data["telephone"] = ph
    if detect_urgent(msg):
        data["_urgent"] = True


def advance_past_captured(session: dict) -> None:
    while session["step"] < len(FLOW):
        key = FLOW_KEYS[session["step"]]
        if session["data"].get(key):
            session["step"] += 1
        else:
            break


def call_llm(message: str, data: dict) -> str:
    if not TOGETHER_API_KEY:
        return f"Je transmets votre message à l'équipe. Pour un retour immédiat, appelez le {COMPANY_PHONE}."
    ctx_lines = []
    for k in ("projet", "surface", "prenom", "telephone"):
        if data.get(k):
            ctx_lines.append(f"- {k} : {data[k]}")
    ctx = "\n".join(ctx_lines) or "(aucun contexte)"
    r = requests.post(
        "https://api.together.xyz/v1/chat/completions",
        headers={"Authorization": f"Bearer {TOGETHER_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": f"Tu es Betty, assistante d'ABC Peinture Déco (Le Mans). Tu réponds en 1 à 2 phrases max, ton chaleureux et direct. Tu orientes toujours vers un rappel rapide au {COMPANY_PHONE}. Jamais robotique, jamais de liste à puces."},
                {"role": "user", "content": f"Contexte client :\n{ctx}\n\nMessage : {message}"},
            ],
            "temperature": 0.6,
            "max_tokens": 120,
        },
        timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f"Together API error {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]["content"].strip()


def test_llm_call() -> dict:
    try:
        answer = call_llm("Réponds uniquement: OK LLM", {})
        return {"ok": True, "model": MODEL, "has_key": bool(TOGETHER_API_KEY), "answer": answer}
    except Exception as e:
        print("ERREUR ABC TEST_LLM:", repr(e), flush=True)
        payload = {"ok": False, "model": MODEL, "has_key": bool(TOGETHER_API_KEY), "error": repr(e)}
        if not DEBUG_BETTY:
            payload["error"] = "hidden; set DEBUG_BETTY=1 to expose details"
        return payload


def _reset_session() -> dict:
    return {"step": 0, "data": {}, "qualified": False, "off_topic_count": 0, "msg_count": 0}


def send_lead(data: dict) -> None:
    webhook_url = os.getenv("LEAD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    clean_data = {"projet": str(data.get("projet") or "").strip(), "surface": str(data.get("surface") or "").strip(), "prenom": str(data.get("prenom") or "").strip(), "telephone": re.sub(r"\D", "", str(data.get("telephone") or "").strip()), "urgent": bool(data.get("_urgent")), "email": ""}
    try:
        requests.post(webhook_url, json={"source": "betty_abc_peinture", "data": clean_data}, timeout=3)
    except Exception:
        pass


def handle_message(user_id: str, message: str) -> str:
    user_id = str(user_id or "anonymous")
    message = str(message or "")
    n = normalize(message)
    is_simple_greeting = is_greeting(message) and len(n.split()) <= 2
    is_reset_cmd = n in {"reset", "recommencer", "restart", "reinit"}
    if is_reset_cmd or is_simple_greeting:
        sessions[user_id] = _reset_session()
        return get_question(0, {})

    s = sessions.setdefault(user_id, _reset_session())
    s["msg_count"] += 1
    opportunistic_capture(s, message)
    advance_past_captured(s)

    def _is_qualified(data):
        return data.get("telephone") and data.get("prenom") and data.get("projet")

    if _is_qualified(s["data"]) and not s["qualified"]:
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()

    if s["qualified"] or s["step"] >= len(FLOW):
        if not s["qualified"]:
            s["qualified"] = True
            send_lead(s["data"])
            return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
        if detect_info_hors_flow(message):
            return recadrage_info_hors_flow(message)
        return call_llm(message, s["data"])

    step_idx = s["step"]
    step_key = FLOW_KEYS[step_idx]
    is_phone_step = step_key == "telephone" and validate_telephone(message)
    is_prenom_step = step_key == "prenom" and validate_prenom(message) and len(n.split()) <= 2
    if detect_info_hors_flow(message) and not is_phone_step and not is_prenom_step:
        return f"{recadrage_info_hors_flow(message)}\n{get_question(step_idx, s['data'])}".strip()

    label = classify_message(message, step_idx)
    if step_key == "projet" and detect_devis_intent(message) and not s["data"].get("projet"):
        s["data"]["projet"] = message.strip()
        s["step"] += 1
        advance_past_captured(s)
        if s["step"] >= len(FLOW):
            s["qualified"] = True
            send_lead(s["data"])
            return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
        return f"Très bien, je note. {get_question(s['step'], s['data'])}".strip()

    if label == "greeting":
        return get_question(step_idx, s["data"])
    if label == "info_hors_flow":
        return f"{recadrage_info_hors_flow(message)}\n{get_question(step_idx, s['data'])}".strip()
    if label == "invalide":
        return f"{recadrage_invalide(step_key)} {get_question(step_idx, s['data'])}".strip()
    if label == "flou":
        s["off_topic_count"] = s.get("off_topic_count", 0) + 1
        if step_key == "projet" and s["off_topic_count"] >= 2:
            s["data"]["projet"] = message.strip() or "à préciser au rappel"
            s["step"] += 1
            s["off_topic_count"] = 0
            advance_past_captured(s)
            if s["step"] >= len(FLOW):
                s["qualified"] = True
                send_lead(s["data"])
                return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
            return f"Pas de souci, on précisera au téléphone. {get_question(s['step'], s['data'])}".strip()
        if s["off_topic_count"] >= MAX_OFFTOPIC:
            s["off_topic_count"] = 0
            return pick(RECAD.get("handover", [f"Laissez-moi votre numéro, on vous rappelle au {COMPANY_PHONE}."]))
        return f"{pick(RECAD.get('flou', ['En quelques mots, quel est votre besoin ?']))} {get_question(step_idx, s['data'])}".strip()

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
    if value or step_key == "surface":
        s["data"][step_key] = value
    s["step"] += 1
    s["off_topic_count"] = 0
    advance_past_captured(s)

    if _is_qualified(s["data"]) and not s["qualified"]:
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
    if s["step"] >= len(FLOW):
        s["qualified"] = True
        send_lead(s["data"])
        return CLOSING_TPL.format(prenom=s["data"].get("prenom") or "", phone=COMPANY_PHONE).strip()
    return f"{get_warmth(step_idx, s['data'])}{get_question(s['step'], s['data'])}".strip()


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
        pass

    def do_OPTIONS(self):
        _json_response(self, {"ok": True})

    def do_GET(self):
        if self.path.startswith("/api/test_llm") or self.path.startswith("/test_llm"):
            return _json_response(self, test_llm_call(), 200)
        _json_response(self, {"ok": True, "bot": CFG["identity"]["name"], "model": MODEL, "has_key": bool(TOGETHER_API_KEY)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            message = str(payload.get("message") or payload.get("text") or "").strip()
            if not message:
                return _json_response(self, {"response": "Je n'ai rien reçu 🤔 Pouvez-vous réessayer ?"})
            raw_session_id = payload.get("session_id") or payload.get("sessionId") or self.client_address[0]
            user_id = str(raw_session_id or "anonymous")
            reply = handle_message(user_id, message)
            return _json_response(self, {"response": reply})
        except Exception as e:
            print("ERREUR ABC BETTY:", repr(e), flush=True)
            payload = {"response": f"Petit bug de mon côté, désolée 😅 Appelez-nous directement au {COMPANY_PHONE}, on s'en occupe tout de suite."}
            if DEBUG_BETTY:
                payload["debug"] = repr(e)
                payload["model"] = MODEL
                payload["has_key"] = bool(TOGETHER_API_KEY)
            return _json_response(self, payload, 500)
