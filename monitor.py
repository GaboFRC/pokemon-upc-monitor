#!/usr/bin/env python3
"""
Monitor de stock para el Pokemon TCG 30th Celebration Ultra-Premium Collection
(Day / Espeon y Night / Umbreon) en weplay.cl.

Lanzamiento oficial: 6 de noviembre de 2026. Puede aparecer antes como preventa.

Fuentes usadas (elegidas tras el reconocimiento de la Fase 1):
  1. Busqueda interna  /catalogsearch/result/?q=...    -> nombre, URL, precio, stock y boton carrito
  2. Listado de marca  ?brand_id=2049 (CARTAS POKEMON) -> respaldo por si no hay match de texto
  3. /preventas.html                                   -> captura la preventa
  4. (opcional) sondeo de URLs candidatas de producto

Respeta robots.txt de weplay.cl: NO se usan parametros product_list_* (estan en Disallow).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from html import unescape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # el .env es opcional
    pass

BASE = "https://www.weplay.cl"
HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- config


def _env(key: str, default: str) -> str:
    return (os.getenv(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, "true" if default else "false").lower() in ("1", "true", "yes", "si", "on")


INTERVAL = int(_env("INTERVAL_MINUTES", "10")) * 60
TIMEOUT = int(_env("REQUEST_TIMEOUT", "30"))
RETRIES = int(_env("MAX_RETRIES", "3"))
PAUSE_MIN = float(_env("PAUSE_MIN_SECONDS", "3"))
PAUSE_MAX = float(_env("PAUSE_MAX_SECONDS", "7"))

STATE_FILE = Path(_env("STATE_FILE", str(HERE / "state.json")))
LOG_FILE = Path(_env("LOG_FILE", str(HERE / "monitor.log")))

NTFY_TOPIC = _env("NTFY_TOPIC", "")
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")

# Terminos de busqueda. "30th" es el mas preciso: en la Fase 1 devolvio 23 resultados
# incluyendo los 6 SKUs de la linea 30th Celebration ya publicados.
SEARCH_TERMS = [t.strip() for t in _env("SEARCH_TERMS", "30th,ultra premium").split(",") if t.strip()]

CHECK_BRAND = _env_bool("CHECK_BRAND_LISTING", True)
CHECK_PREVENTAS = _env_bool("CHECK_PREVENTAS", True)
PROBE_URLS = _env_bool("PROBE_PRODUCT_URLS", False)
PLAYWRIGHT_FALLBACK = _env_bool("PLAYWRIGHT_FALLBACK", False)


def _canon(url: str) -> str:
    """URL comparable: sin query ni fragmento, en minusculas y con dominio."""
    url = url.strip().split("#")[0].split("?")[0].lower()
    if url and not url.startswith("http"):
        url = BASE + "/" + url.lstrip("/")
    return url


# Productos puntuales que se vigilan por URL, sin pasar por la regla del UPC.
# Avisan cuando quedan comprables online (boton de carrito y no "solo en tienda").
DEFAULT_WATCH = BASE + "/pokemon-tcg-30th-celebration-elite-trainer-box-english.html"
WATCH_URLS = [_canon(u) for u in _env("WATCH_URLS", DEFAULT_WATCH).split(",") if u.strip()]

BRAND_URL = BASE + "/juegos-de-mesa-y-cartas/cartas-y-mazos.html?brand_id=2049"
PREVENTAS_URL = BASE + "/preventas.html"

# URLs candidatas. Las 32 variantes probadas en la Fase 1 dieron 404; estas son las mas
# probables segun la convencion real del sitio:
#   pokemon-tcg-30th-celebration-<item>-<espanol|english>
CANDIDATE_URLS = [
    BASE + "/pokemon-tcg-30th-celebration-ultra-premium-collection-espanol.html",
    BASE + "/pokemon-tcg-30th-celebration-ultra-premium-collection-english.html",
    BASE + "/pokemon-tcg-30th-celebration-ultra-premium-collection-day-english.html",
    BASE + "/pokemon-tcg-30th-celebration-ultra-premium-collection-night-english.html",
    BASE + "/pokemon-tcg-30th-celebration-ultra-premium-collection-day-espanol.html",
    BASE + "/pokemon-tcg-30th-celebration-ultra-premium-collection-night-espanol.html",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

log = logging.getLogger("monitor")

# --------------------------------------------------------------------------- logging


def setup_logging(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Windows: evita UnicodeEncodeError con acentos en consolas cp1252
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# --------------------------------------------------------------------------- red

_session = requests.Session()
_session.headers.update(HEADERS)


def polite_pause() -> None:
    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))


def fetch(url: str, allow_404: bool = False) -> str | None:
    """GET con reintentos y backoff exponencial. Devuelve el HTML o None."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = _session.get(url, timeout=TIMEOUT)
            if r.status_code == 404:
                if not allow_404:
                    log.warning("404 en %s", url)
                return None
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as e:
            if attempt == RETRIES:
                log.error("fallo definitivo en %s tras %d intentos: %s", url, RETRIES, e)
                return None
            wait = (2 ** attempt) + random.uniform(0, 1.5)
            log.warning("intento %d/%d fallo en %s (%s); reintento en %.1fs",
                        attempt, RETRIES, url, e, wait)
            time.sleep(wait)
    return None


# --------------------------------------------------------------------------- parsing


def norm(text: str) -> str:
    """minusculas y sin tildes."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower()


def is_target(name: str) -> bool:
    """Regla de coincidencia: ('30th' o 'celebration') junto con 'ultra' y 'premium'."""
    n = norm(name)
    return ("30th" in n or "celebration" in n) and "ultra" in n and "premium" in n


def variant_of(name: str, url: str = "") -> str:
    """Day (Espeon) / Night (Umbreon) / desconocida."""
    blob = norm(name + " " + url)
    if "espeon" in blob or re.search(r"\bday\b", blob):
        return "Day / Espeon"
    if "umbreon" in blob or re.search(r"\bnight\b", blob):
        return "Night / Umbreon"
    return "sin variante identificada"


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", html))).strip()


def compute_status(name: str, url: str, label_cls: str, label_txt: str, cart: bool) -> str:
    """en_stock | preventa | solo_en_tienda | agotado | desconocido"""
    blob = norm(name + " " + url)
    lab = norm(label_txt)

    if "preventa" in blob or "preventa" in lab:
        return "preventa"
    if "agotado" in lab or "sin stock" in lab or "no disponible" in lab:
        return "agotado"
    if "solo en tienda" in lab:
        return "solo_en_tienda"
    if label_cls == "unavailable":
        return "agotado"
    if cart:
        return "en_stock"
    return "desconocido"


def parse_listing(html: str) -> list[dict]:
    """Extrae productos de cualquier grilla de Magento (busqueda, categoria, preventas)."""
    out = []
    # Cada tarjeta es un <li class="item product product-item">. La ultima se corta en
    # </ol> para no arrastrar botones de widgets que haya mas abajo en la pagina.
    cards = re.split(r'<li class="item product product-item"', html)[1:]
    if cards:
        cards[-1] = cards[-1].split("</ol>")[0]
    else:
        cards = re.split(r"product-item-info", html)[1:]

    for block in cards:
        m = (re.search(r'class="product-item-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
             or re.search(r'href="([^"]+)"[^>]+class="product-item-link"[^>]*>(.*?)</a>', block, re.S))
        if not m:
            continue
        url, name = m.group(1), _strip_tags(m.group(2))
        if not name:
            continue

        price_m = re.search(r'data-price-amount="([0-9.]+)"', block)
        price = int(float(price_m.group(1))) if price_m else None

        label_m = re.search(r'class="product-label stock ([a-z-]+)">\s*<span>([^<]*)</span>', block)
        label_cls, label_txt = (label_m.group(1), label_m.group(2).strip()) if label_m else ("", "")

        cart = bool(re.search(r"\btocart\b", block))

        out.append({
            "name": name,
            "url": url,
            "price": price,
            "cart": cart,
            "status": compute_status(name, url, label_cls, label_txt, cart),
            "label": label_txt,
        })
    return out


def parse_product_page(html: str, url: str) -> dict | None:
    """
    La ficha de producto de weplay NO trae el stock server-side (comprobado en la Fase 1),
    pero si trae nombre y precio. Sirve para confirmar que la URL existe.
    """
    if "catalog-product-view" not in html:
        return None
    name_m = re.search(r"<title>([^<]*)</title>", html)
    price_m = re.search(
        r'(?:property|name)="(?:product:price:amount|og:price:amount)" content="([^"]*)"', html)
    return {
        "name": _strip_tags(name_m.group(1)) if name_m else url,
        "url": url,
        "price": int(float(price_m.group(1))) if price_m else None,
        "cart": False,
        "status": "desconocido",
        "label": "",
    }


def stock_via_playwright(url: str) -> dict | None:
    """Solo para la ficha de producto, cuando el HTML plano no revela el stock."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("PLAYWRIGHT_FALLBACK activo pero playwright no esta instalado")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=UA, locale="es-CL")
            page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            body = page.content()
            try:
                btn = page.query_selector("#product-addtocart-button")
                cart = bool(btn and btn.is_enabled())
            except Exception:
                cart = False
            browser.close()
    except Exception as e:
        log.error("playwright fallo en %s: %s", url, e)
        return None

    label_m = re.search(r'class="product-label stock ([a-z-]+)">\s*<span>([^<]*)</span>', body)
    label_cls, label_txt = (label_m.group(1), label_m.group(2).strip()) if label_m else ("", "")
    name_m = re.search(r"<title>([^<]*)</title>", body)
    name = _strip_tags(name_m.group(1)) if name_m else url
    price_m = re.search(r'(?:property|name)="product:price:amount" content="([^"]*)"', body)

    return {
        "name": name,
        "url": url,
        "price": int(float(price_m.group(1))) if price_m else None,
        "cart": cart,
        "status": compute_status(name, url, label_cls, label_txt, cart),
        "label": label_txt,
    }


# --------------------------------------------------------------------------- fuentes


def collect() -> tuple[list[dict], int, int]:
    """
    Recorre las fuentes y devuelve (productos que calzan, listados pedidos, listados que
    respondieron). Los dos contadores permiten distinguir "no hay cambios" de "weplay no
    nos deja entrar".
    """
    found: dict[str, dict] = {}
    requests_made = 0
    listings_tried = 0
    listings_ok = 0

    def get_listing(url):
        nonlocal requests_made, listings_tried, listings_ok
        html = fetch(url)
        requests_made += 1
        listings_tried += 1
        if html:
            listings_ok += 1
        return html

    watch_set = set(WATCH_URLS)

    def absorb(items, source):
        for it in items:
            key = _canon(it["url"])
            watched = key in watch_set
            if not (is_target(it["name"]) or watched):
                continue
            it["url"] = key
            it["source"] = source
            it["watched"] = watched
            it["variant"] = variant_of(it["name"], it["url"]) if not watched else "-"
            prev = found.get(key)
            # Las paginas de weplay pasan por cache y dos fuentes pueden discrepar unos
            # minutos. Si alguna lo muestra comprable online, se prefiere esa: mejor un
            # aviso de mas que perderse el stock.
            if (prev is None
                    or (prev["status"] == "desconocido" and it["status"] != "desconocido")
                    or (is_online(it) and not is_online(prev))):
                found[key] = it
            log.info("%s [%s] %s | %s | carrito=%s",
                     "VIGILADO" if watched else "MATCH", source, it["name"], it["status"], it["cart"])

    for term in SEARCH_TERMS:
        html = get_listing(BASE + "/catalogsearch/result/?q=" + quote_plus(term))
        if html:
            items = parse_listing(html)
            log.info("busqueda '%s': %d productos", term, len(items))
            absorb(items, "busqueda:" + term)
        polite_pause()

    if CHECK_BRAND:
        html = get_listing(BRAND_URL)
        if html:
            items = parse_listing(html)
            log.info("listado marca Pokemon: %d productos", len(items))
            absorb(items, "brand_id=2049")
        polite_pause()

    if CHECK_PREVENTAS:
        html = get_listing(PREVENTAS_URL)
        if html:
            items = parse_listing(html)
            log.info("preventas: %d productos", len(items))
            absorb(items, "preventas")
        polite_pause()

    if PROBE_URLS:
        for url in CANDIDATE_URLS:
            html = fetch(url, allow_404=True)
            requests_made += 1
            if html:
                item = parse_product_page(html, url)
                if item:
                    log.info("URL candidata VIVA: %s", url)
                    absorb([item], "sondeo-url")
            polite_pause()

    # Un producto vigilado que no salio en ninguna fuente: una busqueda extra con las
    # palabras de su URL. La ficha de producto no sirve aqui porque no trae el stock.
    for url in WATCH_URLS:
        if url in found:
            continue
        if listings_tried and not listings_ok:
            break  # si no respondio ninguna pagina, insistir solo suma peticiones rechazadas
        query = url.rsplit("/", 1)[-1].removesuffix(".html").replace("-", " ")
        html = get_listing(BASE + "/catalogsearch/result/?q=" + quote_plus(query))
        if html:
            absorb(parse_listing(html), "busqueda-vigilado")
        polite_pause()
        if url not in found:
            log.warning("no vi %s en ninguna fuente; conservo su estado anterior", url)

    if PLAYWRIGHT_FALLBACK:
        for url, it in list(found.items()):
            if it["status"] == "desconocido":
                log.info("playwright: resolviendo stock de %s", url)
                better = stock_via_playwright(url)
                if better:
                    it.update({k: better[k] for k in ("price", "cart", "status", "label")})

    log.info("ciclo: %d peticiones, %d/%d listados respondieron, %d coincidencias",
             requests_made, listings_ok, listings_tried, len(found))
    return list(found.values()), listings_tried, listings_ok


# --------------------------------------------------------------------------- estado


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.error("state.json ilegible (%s); empiezo de cero", e)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


BUYABLE = {"en_stock", "preventa"}


def is_online(it: dict) -> bool:
    """Comprable por la web: boton de carrito y sin etiqueta de tienda o agotado."""
    return bool(it.get("cart")) and it.get("status") not in ("solo_en_tienda", "agotado")


def clp(value) -> str:
    return "$" + format(value, ",").replace(",", ".") if value else "sin precio"


def diff(old: dict, items: list[dict]) -> tuple[list[str], dict]:
    """Devuelve (avisos, estado nuevo). Solo avisa cuando algo cambia de verdad."""
    alerts = []
    # Lo que no se vio este ciclo se conserva: si no, al reaparecer se avisaria
    # como producto nuevo.
    new_state = dict(old)

    for it in items:
        url = it["url"]
        prev = old.get(url)
        new_state[url] = {k: it.get(k) for k in ("name", "price", "status", "cart", "variant", "watched")}
        boton = "habilitado" if it["cart"] else "deshabilitado"

        if prev is None:
            if it.get("watched") and not is_online(it):
                # producto conocido que se empieza a vigilar: registrar sin molestar
                log.info("empiezo a vigilar %s (hoy: %s)", it["name"], it["status"])
                continue
            header = "DISPONIBLE PARA COMPRA ONLINE" if it.get("watched") else "NUEVO PRODUCTO DETECTADO"
            alerts.append(
                header + "\n" + it["name"]
                + ("" if it.get("watched") else "\nVariante: " + it["variant"])
                + "\nEstado: " + it["status"] + " | Agregar al carrito: " + boton
                + "\nPrecio: " + clp(it["price"])
                + "\n" + url)
            continue

        if is_online(it) and not is_online(prev):
            alerts.append(
                "DISPONIBLE PARA COMPRA ONLINE (antes: " + str(prev.get("status")) + ")\n"
                + it["name"]
                + "\nPrecio: " + clp(it["price"])
                + "\n" + url)
        elif prev.get("status") != it["status"] and it["status"] in BUYABLE:
            alerts.append(
                "CAMBIO DE ESTADO: " + str(prev.get("status")) + " -> " + it["status"].upper()
                + "\n" + it["name"]
                + "\nAgregar al carrito: " + boton
                + "\nPrecio: " + clp(it["price"])
                + "\n" + url)
        elif is_online(prev) and not is_online(it):
            log.info("%s dejo de estar disponible online (ahora: %s)", it["name"], it["status"])

        if prev.get("price") and it["price"] and prev["price"] != it["price"]:
            alerts.append(
                "CAMBIO DE PRECIO: " + clp(prev["price"]) + " -> " + clp(it["price"])
                + "\n" + it["name"] + "\n" + url)

    return alerts, new_state


# --------------------------------------------------------------------------- avisos


def notify_ntfy(title: str, message: str, priority: str = "high") -> bool:
    if not NTFY_TOPIC:
        return False
    try:
        r = requests.post(
            NTFY_SERVER + "/" + NTFY_TOPIC,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "rotating_light",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        log.info("ntfy enviado: %s", title)
        return True
    except requests.RequestException as e:
        log.error("ntfy fallo: %s", e)
        return False


def notify_telegram(title: str, message: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": title + "\n\n" + message,
                "disable_web_page_preview": False,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        log.info("telegram enviado: %s", title)
        return True
    except requests.RequestException as e:
        log.error("telegram fallo: %s", e)
        return False


def notify(title: str, message: str) -> None:
    sent_ntfy = notify_ntfy(title, message)
    sent_tg = notify_telegram(title, message)
    if not (sent_ntfy or sent_tg):
        log.warning("sin canal de notificacion configurado; el aviso solo queda en el log")


# --------------------------------------------------------------------------- ciclo


META = "__monitor__"


def run_once() -> int:
    """Un ciclo. Devuelve la cantidad de avisos, o -1 si weplay no respondio nada."""
    items, tried, ok = collect()
    old = load_state()
    meta = old.pop(META, {})

    if tried and not ok:
        log.error("weplay no respondio ninguna pagina (%d de %d fallaron): posible bloqueo. "
                  "Nada de lo vigilado se pudo revisar.", tried, tried)
        if not meta.get("sin_acceso"):
            notify("Monitor weplay.cl SIN ACCESO",
                   "weplay.cl rechazo todas las revisiones de este ciclo, asi que el monitor "
                   "NO esta vigilando. Te aviso de nuevo cuando recupere el acceso.")
        meta["sin_acceso"] = True
        old[META] = meta
        save_state(old)
        return -1

    if meta.get("sin_acceso"):
        notify("Monitor weplay.cl recupero el acceso",
               "weplay.cl volvio a responder y el monitor sigue vigilando.")
    meta["sin_acceso"] = False
    if ok < tried:
        log.warning("%d de %d listados no respondieron este ciclo", tried - ok, tried)

    alerts, new_state = diff(old, items)

    if alerts:
        for a in alerts:
            log.info("AVISO:\n%s", a)
        notify("Pokemon 30th Celebration - weplay.cl",
               "\n\n---\n\n".join(alerts))
    else:
        log.info("sin cambios (%d productos vigilados)", len(new_state))

    new_state[META] = meta
    save_state(new_state)
    return len(alerts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Monitor del Pokemon TCG 30th Celebration Ultra-Premium Collection en weplay.cl")
    ap.add_argument("--test", action="store_true",
                    help="envia una notificacion de prueba y sale")
    ap.add_argument("--once", action="store_true",
                    help="un solo ciclo y sale (para cron / Programador de tareas / GitHub Actions)")
    ap.add_argument("--interval", type=int, metavar="MIN",
                    help="minutos entre ciclos (default: " + str(INTERVAL // 60) + ")")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    if args.test:
        log.info("modo test: enviando notificacion de prueba")
        notify("Prueba - monitor weplay.cl",
               "Si lees esto, las notificaciones funcionan.\n\n"
               + "ntfy: " + (NTFY_TOPIC if NTFY_TOPIC else "no configurado") + "\n"
               + "telegram: " + ("configurado" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "no configurado"))
        return 0

    if args.once:
        # codigo 1 si no hubo acceso: el Programador de tareas o GitHub lo marcan como fallo
        return 1 if run_once() < 0 else 0

    interval = args.interval * 60 if args.interval else INTERVAL
    log.info("monitor iniciado - revision cada %d minutos (Ctrl+C para salir)", interval // 60)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log.info("detenido por el usuario")
            return 0
        except Exception:
            log.exception("error inesperado en el ciclo; sigo al siguiente")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
