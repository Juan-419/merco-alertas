import os
import json
import re
import requests
import logging
import threading
from urllib.parse import urljoin
from datetime import datetime

from flask import Flask, jsonify
from playwright.sync_api import sync_playwright
import pytz



# ── Render / Playwright ────────────────────────────────────────────────────────
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/.playwright")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

lock_revision = threading.Lock()

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

OFERTAS_URL = "https://mercoapp.com/categoria-producto/ofertas/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.getenv("STATE_FILE", "ofertas_state.json")
STATE_PATH = STATE_FILE if os.path.isabs(STATE_FILE) else os.path.join(BASE_DIR, STATE_FILE)

CHROMIUM_PATH = os.getenv(
    "PLAYWRIGHT_CHROMIUM_PATH",
    "/opt/render/project/.playwright/chromium-1187/chrome-linux/chrome"
)


def validar_configuracion():
    faltantes = []

    if not TELEGRAM_BOT_TOKEN:
        faltantes.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        faltantes.append("TELEGRAM_CHAT_ID")

    if faltantes:
        log.warning(
            f"Faltan variables de entorno: {', '.join(faltantes)}"
        )

def parsear_precio(texto):
    if not texto:
        return None
    limpio = re.sub(r"[^\d,.]", "", texto)
    limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


def variacion_pct(antes, despues):
    if antes in (None, 0):
        return ""
    pct = ((despues - antes) / antes) * 100
    signo = "+" if pct > 0 else ""
    return f"{signo}{pct:.1f} %"


def cargar_estado():
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"No se pudo cargar el estado: {e}")
    return {}


def guardar_estado(ofertas):
    try:
        dirpath = os.path.dirname(STATE_PATH)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(ofertas, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"No se pudo guardar el estado: {e}")
        raise


def scrapear_ofertas():
    productos = {}

    try:
        with sync_playwright() as p:
            log.info(f"Usando Chromium: {CHROMIUM_PATH}")

            browser = p.chromium.launch(
                executable_path=CHROMIUM_PATH,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            log.info("Chromium abierto correctamente")

            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.6367.82 Mobile Safari/537.36"
                    ),
                    locale="es-CO",
                    viewport={"width": 390, "height": 844},
                )
                log.info("Contexto creado")

                page = context.new_page()
                log.info("Página creada")


                log.info("Entrando directamente a ofertas...")
                page.goto(
                    OFERTAS_URL,
                    wait_until="networkidle",
                    timeout=60000
                )

                page.wait_for_timeout(5000)

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)

                # Espera razonable para que aparezcan productos
                try:
                    page.wait_for_selector(
                        "li.product, article.product, div.product",
                        timeout=15000
                    )
                except Exception:
                    log.warning("No apareció el selector de productos a tiempo")

                items = page.query_selector_all("li.product, article.product, div.product")
                log.info(f"Elementos producto encontrados: {len(items)}")

                for item in items:
                    try:
                        nombre_tag = item.query_selector(
                            ".woocommerce-loop-product__title, h2, h3"
                        )
                        if not nombre_tag:
                            continue

                        nombre = nombre_tag.inner_text().strip()
                        if not nombre:
                            continue

                        precio_ins = item.query_selector("ins .woocommerce-Price-amount, ins bdi")
                        precio_del = item.query_selector("del .woocommerce-Price-amount, del bdi")
                        precio_unico = item.query_selector(
                            ".woocommerce-Price-amount:not(ins *):not(del *), bdi:not(ins *):not(del *)"
                        )

                        link_tag = item.query_selector("a.woocommerce-loop-product__link, a")
                        img_tag = item.query_selector("img")

                        precio_nuevo_txt = precio_ins.inner_text().strip() if precio_ins else None
                        precio_original_txt = precio_del.inner_text().strip() if precio_del else None
                        precio_unico_txt = precio_unico.inner_text().strip() if precio_unico else None
                        precio_display = precio_nuevo_txt or precio_unico_txt

                        link = OFERTAS_URL
                        if link_tag:
                            href = link_tag.get_attribute("href")
                            if href:
                                link = urljoin(OFERTAS_URL, href)

                        imagen = ""
                        if img_tag:
                            imagen = (
                                img_tag.get_attribute("src")
                                or img_tag.get_attribute("data-src")
                                or ""
                            )

                        productos[nombre] = {
                            "precio_nuevo": precio_nuevo_txt,
                            "precio_original": precio_original_txt,
                            "precio_display": precio_display,
                            "link": link,
                            "imagen": imagen,
                        }

                    except Exception as e:
                        log.warning(f"Error procesando un producto: {e}")

            finally:
                browser.close()
                log.info("Chromium cerrado")

    except Exception:
        log.exception("Error en scrapear_ofertas()")

    log.info(f"Ofertas scrapeadas: {len(productos)}")
    return productos


def detectar_cambios(anterior, actual):
    nuevas = {}
    precio_baja = {}
    precio_sube = {}

    for nombre, datos in actual.items():
        if nombre not in anterior:
            nuevas[nombre] = datos
            continue

        val_antes = parsear_precio(anterior[nombre].get("precio_display"))
        val_despues = parsear_precio(datos.get("precio_display"))

        if val_antes is None or val_despues is None:
            continue

        if val_despues < val_antes:
            precio_baja[nombre] = {
                **datos,
                "precio_antes_txt": anterior[nombre].get("precio_display"),
                "precio_ahora_txt": datos.get("precio_display"),
                "variacion": variacion_pct(val_antes, val_despues),
            }
        elif val_despues > val_antes:
            precio_sube[nombre] = {
                **datos,
                "precio_antes_txt": anterior[nombre].get("precio_display"),
                "precio_ahora_txt": datos.get("precio_display"),
                "variacion": variacion_pct(val_antes, val_despues),
            }

    return nuevas, precio_baja, precio_sube


def _tabla(filas_html, columnas):
    ths = "".join(
        f'<th style="padding:10px 8px;text-align:left;">{c}</th>'
        for c in columnas
    )
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;font-size:14px;margin-top:8px;">
      <thead>
        <tr style="background:#fafafa;color:#888;font-size:12px;text-transform:uppercase;">
          {ths}
        </tr>
      </thead>
      <tbody>{filas_html}</tbody>
    </table>
    """


def _fila_nueva(nombre, d):
    link = d.get("link", OFERTAS_URL)
    return f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:11px 8px;font-weight:600;color:#1a1a1a;">{nombre}</td>
      <td style="padding:11px 8px;color:#888;text-decoration:line-through;">{d.get('precio_original') or '—'}</td>
      <td style="padding:11px 8px;color:#e53935;font-weight:700;">{d.get('precio_nuevo') or d.get('precio_display') or '—'}</td>
      <td style="padding:11px 8px;">
        <a href="{link}" style="background:#e53935;color:#fff;padding:5px 12px;border-radius:6px;text-decoration:none;font-size:13px;">Ver →</a>
      </td>
    </tr>
    """


def _fila_cambio(nombre, d, es_baja):
    color = "#2e7d32" if es_baja else "#b71c1c"
    icono = "📉" if es_baja else "📈"
    link = d.get("link", OFERTAS_URL)
    return f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:11px 8px;font-weight:600;color:#1a1a1a;">{nombre}</td>
      <td style="padding:11px 8px;color:#888;">{d.get('precio_antes_txt') or '—'}</td>
      <td style="padding:11px 8px;color:{color};font-weight:700;">{d.get('precio_ahora_txt') or '—'}</td>
      <td style="padding:11px 8px;color:{color};font-weight:600;">{icono} {d.get('variacion', '')}</td>
      <td style="padding:11px 8px;">
        <a href="{link}" style="background:{color};color:#fff;padding:5px 12px;border-radius:6px;text-decoration:none;font-size:13px;">Ver →</a>
      </td>
    </tr>
    """


def construir_seccion(titulo, icono, color, items, tipo):
    if not items:
        return ""

    if tipo == "nueva":
        filas = "".join(_fila_nueva(n, d) for n, d in items.items())
        tabla = _tabla(filas, ["Producto", "Precio original", "Precio oferta", ""])
    else:
        es_baja = tipo == "baja"
        filas = "".join(_fila_cambio(n, d, es_baja) for n, d in items.items())
        tabla = _tabla(filas, ["Producto", "Precio anterior", "Precio nuevo", "Variación", ""])

    return f"""
    <h2 style="color:{color};margin:32px 0 4px;">{icono} {titulo}
      <span style="font-size:14px;font-weight:400;color:#888;">({len(items)} producto{'s' if len(items) != 1 else ''})</span>
    </h2>
    {tabla}
    """


def _telegram_api(method, payload):
    """Llama a cualquier método de la API de Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado, se omite envío")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=30)
        if not r.ok:
            log.error(f"Telegram {method} falló: {r.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Error en Telegram {method}: {e}")
        return False


def enviar_telegram(mensaje):
    """Envía un mensaje de texto simple."""
    _telegram_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    log.info("Mensaje enviado a Telegram")


def enviar_telegram_foto(imagen_url, caption):
    """
    Envía una foto con caption. Si la URL de imagen falla,
    cae back a sendMessage con el caption igual.
    """
    ok = _telegram_api("sendPhoto", {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": imagen_url,
        "caption": caption,
        "parse_mode": "HTML",
    })
    if not ok:
        # Fallback: mensaje de texto si la foto no cargó
        _telegram_api("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })


def _caption_producto(nombre, d, tipo):
    """
    Arma el caption HTML para un producto individual.
    tipo: 'nueva' | 'baja' | 'sube'
    """
    link = d.get("link", OFERTAS_URL)

    if tipo == "nueva":
        precio_antes = d.get("precio_original")
        precio_ahora = d.get("precio_nuevo") or d.get("precio_display") or "—"
        encabezado = "🆕 <b>Nueva oferta</b>"
        linea_precio = f"💰 Precio: <b>{precio_ahora}</b>"
        if precio_antes:
            linea_precio += f"\n<s>{precio_antes}</s>"

    elif tipo == "baja":
        encabezado = "📉 <b>Bajó de precio</b>"
        linea_precio = (
            f"💰 Antes: <s>{d.get('precio_antes_txt', '—')}</s>\n"
            f"✅ Ahora: <b>{d.get('precio_ahora_txt', '—')}</b>  "
            f"({d.get('variacion', '')})"
        )
    else:  # sube
        encabezado = "📈 <b>Subió de precio</b>"
        linea_precio = (
            f"💰 Antes: {d.get('precio_antes_txt', '—')}\n"
            f"⚠️ Ahora: <b>{d.get('precio_ahora_txt', '—')}</b>  "
            f"({d.get('variacion', '')})"
        )

    return (
        f"{encabezado}\n"
        f"📦 {nombre}\n"
        f"{linea_precio}\n"
        f"🔗 <a href=\"{link}\">Ver producto</a>"
    )


def notificar_grupo(titulo, items, tipo):
    """Envía un mensaje por cada producto del grupo, con foto si tiene."""
    if not items:
        return
    # Encabezado del grupo
    enviar_telegram(f"{titulo} — <b>{len(items)} producto{'s' if len(items)!=1 else ''}</b>")
    for nombre, d in items.items():
        caption = _caption_producto(nombre, d, tipo)
        imagen  = d.get("imagen", "")
        if imagen and imagen.startswith("http"):
            enviar_telegram_foto(imagen, caption)
        else:
            enviar_telegram(caption)


def construir_resumen_telegram(nuevas, precio_baja, precio_sube, total):
    """Mensaje inicial con el resumen general."""
    partes = ["🛒 <b>MercoApp · Revisión de ofertas</b>", ""]
    partes.append(f"📦 Total en oferta: <b>{total}</b>")

    if not (nuevas or precio_baja or precio_sube):
        partes.append("Sin novedades esta semana 😴")
        return "\n".join(partes)

    if nuevas:
        partes.append(f"🆕 Nuevas: <b>{len(nuevas)}</b>")
    if precio_baja:
        partes.append(f"📉 Bajaron: <b>{len(precio_baja)}</b>")
    if precio_sube:
        partes.append(f"📈 Subieron: <b>{len(precio_sube)}</b>")

    return "\n".join(partes)

def revisar_ofertas():
    if not lock_revision.acquire(blocking=False):
        return {
            "status": "ocupado",
            "mensaje": "Ya hay una revisión en curso"
        }

    try:
        log.info("=== Revisión MercoApp iniciada ===")

        anterior = cargar_estado()
        actual = scrapear_ofertas()

        if not actual:
            log.warning("No se obtuvieron ofertas. No se envía notificación.")
            return {
                "status": "sin_datos",
                "mensaje": "No se pudieron scrapear ofertas."
            }

        nuevas, precio_baja, precio_sube = detectar_cambios(
            anterior,
            actual
        )

        if not (nuevas or precio_baja or precio_sube):
            log.info("Sin cambios detectados. No se envía notificación.")
            guardar_estado(actual)
            return {
                "status": "sin_cambios",
                "total": len(actual)
            }

        # Resumen general
        enviar_telegram(construir_resumen_telegram(nuevas, precio_baja, precio_sube, len(actual)))

        # Un mensaje por producto con foto
        notificar_grupo("🆕 Nuevas ofertas",      nuevas,      "nueva")
        notificar_grupo("📉 Precios que bajaron", precio_baja, "baja")
        notificar_grupo("📈 Precios que subieron", precio_sube, "sube")

        guardar_estado(actual)

        resultado = {
            "status": "ok",
            "nuevas": len(nuevas),
            "bajaron": len(precio_baja),
            "subieron": len(precio_sube),
            "total": len(actual)
        }

        log.info(
            f"Listo. Nuevas: {len(nuevas)} | "
            f"Bajaron: {len(precio_baja)} | "
            f"Subieron: {len(precio_sube)}"
        )

        return resultado

    finally:
        lock_revision.release()

# ── Scheduler semanal (sábados 10:00 a.m. Bogotá) ────────────────────────────

def _loop_scheduler():
    bogota = pytz.timezone("America/Bogota")
    ejecutado_hoy = None

    while True:
        ahora     = datetime.now(bogota)
        es_sabado = ahora.weekday() == 5
        es_hora   = ahora.hour == 10 and ahora.minute == 0
        fecha_hoy = ahora.date()

        if es_sabado and es_hora and ejecutado_hoy != fecha_hoy:
            ejecutado_hoy = fecha_hoy
            try:
                revisar_ofertas()
            except Exception as e:
                log.error(f"Error en job programado: {e}")

        import time
        time.sleep(55)

_hilo_scheduler = threading.Thread(target=_loop_scheduler, daemon=True)
_hilo_scheduler.start()
log.info("Scheduler activo → cada sábado 10:00 a.m. (Bogotá)")


@app.route("/")
def index():
    return jsonify({
        "status": "activo",
        "descripcion": "Bot de alertas de ofertas MercoApp",
        "detecta": ["nuevas ofertas", "bajadas de precio", "subidas de precio"],
        "ofertas_en_cache": len(cargar_estado()),
        "url_monitorizada": OFERTAS_URL,
        "nota": "Usa /revisar-ahora para disparar una revisión manual."
    })

@app.route("/revisar-ahora")
def revisar_ahora():
    try:
        resultado = revisar_ofertas()
        return jsonify(resultado)
    except Exception as e:
        log.exception("Error en /revisar-ahora")
        return jsonify({
            "status": "error",
            "detalle": str(e)
        }), 500

@app.route("/estado")
def estado():
    ofertas = cargar_estado()
    return jsonify({
        "total": len(ofertas),
        "ofertas": ofertas
    })


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    validar_configuracion()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)