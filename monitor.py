"""
Surveille la bourse de revente des 24h du Mans et envoie une notification
(ntfy) dès qu'une nouvelle annonce contenant "Houx" (Houx ou Houx Annexe)
apparaît.

Variables d'environnement :
  NTFY_TOPIC         (obligatoire) nom du "topic" ntfy, à garder secret
  NTFY_SERVER        (optionnel) défaut https://ntfy.sh
  KEYWORDS           (optionnel) mots-clés séparés par des virgules, défaut "houx"
  DURATION_MINUTES   (optionnel) durée de la boucle ; 0 = une seule vérification
  INTERVAL_SECONDS   (optionnel) pause entre deux vérifications, défaut 120
  STATE_FILE         (optionnel) défaut state.json
"""

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://ticketchange.24h-lemans.com/list/resaleProducts/?lang=fr&page={page}"
MAX_PAGES = 15
EMPTY_MARKERS = ("aucun billet en revente", "aucun produit ne correspond")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
KEYWORDS = [k.strip().casefold() for k in os.environ.get("KEYWORDS", "houx").split(",") if k.strip()]
DURATION_MINUTES = float(os.environ.get("DURATION_MINUTES", "0"))
INTERVAL_SECONDS = max(60, int(os.environ.get("INTERVAL_SECONDS", "120")))  # jamais moins d'1 min
STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# ---------------------------------------------------------------- analyse ---

def extract_matches(page_text: str, keywords=None, context_after: int = 2):
    """Renvoie une liste de (ligne_clé, extrait) pour chaque ligne contenant un mot-clé.
    L'extrait inclut quelques lignes suivantes (souvent le prix / la date)."""
    keywords = keywords or KEYWORDS
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if any(k in line.casefold() for k in keywords):
            snippet = " | ".join(lines[i:i + 1 + context_after])
            out.append((line, snippet))
    return out


def is_empty_page(page_text: str) -> bool:
    t = page_text.casefold()
    return any(m in t for m in EMPTY_MARKERS)


def new_listings(previous: Counter, current: Counter) -> list:
    """Clés dont le nombre d'occurrences a augmenté (nouvelle annonce)."""
    return [k for k, n in current.items() if n > previous.get(k, 0)]


# ------------------------------------------------------------- navigateur ---

def fetch_all_pages(browser) -> list:
    """Parcourt les pages de revente et renvoie le texte de chacune."""
    texts = []
    context = browser.new_context(locale="fr-FR")
    page = context.new_page()
    try:
        for n in range(1, MAX_PAGES + 1):
            page.goto(BASE_URL.format(page=n), wait_until="networkidle", timeout=60_000)
            # attendre la fin du "Chargement" (best effort)
            try:
                page.wait_for_function(
                    "() => !document.body.innerText.includes('Chargement')", timeout=15_000
                )
            except Exception:
                pass
            page.wait_for_timeout(1500)
            text = page.inner_text("body")
            if is_empty_page(text) or (texts and text == texts[-1]):
                break
            texts.append(text)
    finally:
        context.close()
    return texts


# ---------------------------------------------------------- notifications ---

def notify(title: str, message: str, priority: str = "urgent", tags: str = "rotating_light") -> None:
    if not NTFY_TOPIC:
        log(f"(pas de NTFY_TOPIC) {title} : {message}")
        return
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("latin-1", "replace").decode("latin-1"),
                "Priority": priority,
                "Tags": tags,
                "Click": BASE_URL.format(page=1),
            },
            timeout=15,
        ).raise_for_status()
    except Exception as e:  # une notif ratée ne doit pas tuer la boucle
        log(f"Echec envoi ntfy : {e}")


# ------------------------------------------------------------------ état ---

def load_state() -> Counter:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return Counter(json.load(f).get("seen", {}))
    except (FileNotFoundError, json.JSONDecodeError):
        return Counter()


def save_state(seen: Counter) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": dict(seen)}, f, ensure_ascii=False, indent=2, sort_keys=True)


# ------------------------------------------------------------------ main ---

def check_once(browser, seen: Counter) -> Counter:
    texts = fetch_all_pages(browser)
    current = Counter()
    snippets = {}
    for t in texts:
        for key, snippet in extract_matches(t):
            current[key] += 1
            snippets.setdefault(key, snippet)

    fresh = new_listings(seen, current)
    log(f"{len(texts)} page(s) lue(s), {sum(current.values())} annonce(s) Houx, {len(fresh)} nouvelle(s)")
    if fresh:
        body = "\n".join(f"- {snippets[k]}" for k in fresh)
        notify("Place HOUX en revente aux 24h !", body + "\n\nVite, va sur le site !")
    return current  # l'état reflète ce qui est en ligne maintenant


def main() -> int:
    if not NTFY_TOPIC:
        log("ATTENTION : NTFY_TOPIC non défini, les alertes s'afficheront seulement dans les logs.")

    seen = load_state()
    deadline = time.monotonic() + DURATION_MINUTES * 60
    consecutive_errors = 0
    error_notified = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            while True:
                try:
                    seen = check_once(browser, seen)
                    save_state(seen)
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    log(f"Erreur ({consecutive_errors}) : {e}")
                    if consecutive_errors >= 3 and not error_notified:
                        notify("Alerte Houx : le site ne repond plus",
                               f"3 echecs d'affilee. Derniere erreur : {e}",
                               priority="default", tags="warning")
                        error_notified = True

                if time.monotonic() + INTERVAL_SECONDS > deadline:
                    break
                time.sleep(INTERVAL_SECONDS)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
