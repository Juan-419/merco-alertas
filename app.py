import os
import json
import re
import requests
import logging
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

                log.info("Entrando a home...")
                page.goto(
                    "https://mercoapp.com/",
                    wait_until="domcontentloaded",
                    timeout=30000
                )
                page.wait_for_timeout(2000)

                log.info("Entrando a ofertas...")
                page.goto(
                    OFERTAS_URL,
                    wait_until="domcontentloaded",
                    timeout=30000
                )

                page.wait_for_timeout(3000)
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

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
<table width="100%" bgcolor="#f5f5f5" cellpadding="0" cellspacing="0">
<tr>
  <td align="center" style="padding:30px 10px;">
    <table width="640" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:100%;">
      <tr>
        <td style="background:#e53935;padding:28px 32px;text-align:center;">
          <h1 style="color:#fff;margin:0;font-size:22px;">🛒 MercoApp · Alerta de Ofertas</h1>
          <p style="color:rgba(255,255,255,.85);margin:6px 0 0;font-size:13px;">{fecha}</p>
        </td>
      </tr>
      <tr>
        <td style="background:#fff8f8;padding:16px 32px;border-bottom:1px solid #fdecea;">
          <p style="margin:0;color:#555;font-size:14px;">📦 Total en oferta: <strong>{len(todas)}</strong> &nbsp;·&nbsp; {resumen_badge}</p>
        </td>
      </tr>
      <tr>
        <td style="padding:24px 32px;">
          {sec_nuevas}
          {sec_baja}
          {sec_sube}
          {sin_nov}
          <div style="margin-top:36px;text-align:center;">
            <a href="{OFERTAS_URL}" style="background:#e53935;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;">Ver todas las ofertas →</a>
          </div>
        </td>
      </tr>
      <tr>
        <td style="background:#fafafa;padding:16px 32px;text-align:center;color:#bbb;font-size:12px;border-top:1px solid #f0f0f0;">
          Bot automático de MercoApp 🤖
        </td>
      </tr>
    </table>
  </td>
</tr>
</table>
</body>
</html>"""


def enviar_telegram(mensaje):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID"
        )

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML"
    }

    r = requests.post(url, json=payload, timeout=30)

    if not r.ok:
        raise RuntimeError(
            f"Telegram respondió: {r.text}"
        )

    log.info("Mensaje enviado a Telegram")


def construir_mensaje_telegram(
    nuevas,
    precio_baja,
    precio_sube,
    total
):
    partes = []

    partes.append("🛒 <b>MercoApp</b>")
    partes.append("")
    partes.append(f"📦 Total ofertas: {total}")

    if nuevas:
        partes.append(f"🆕 Nuevas ofertas: {len(nuevas)}")

    if precio_baja:
        partes.append(
            f"📉 Bajaron de precio: {len(precio_baja)}"
        )

    if precio_sube:
        partes.append(
            f"📈 Subieron de precio: {len(precio_sube)}"
        )

    partes.append("")
    partes.append(
        "🔗 https://mercoapp.com/categoria-producto/ofertas/"
    )


    return "\n".join(partes)

def revisar_ofertas():
    log.info("=== Revisión MercoApp iniciada ===")

    anterior = cargar_estado()
    actual = scrapear_ofertas()

    if not actual:
        log.warning("No se obtuvieron ofertas. No se envía notificación.")
        return {
            "status": "sin_datos",
            "mensaje": "No se pudieron scrapear ofertas."
        }

    nuevas, precio_baja, precio_sube = detectar_cambios(anterior, actual)

    if not (nuevas or precio_baja or precio_sube):
        log.info("Sin cambios detectados. No se envía notificación.")
        guardar_estado(actual)
        return {
            "status": "sin_cambios",
            "total": len(actual)
        }

    mensaje = construir_mensaje_telegram(
    nuevas,
    precio_baja,
    precio_sube,
    len(actual)
    )

    enviar_telegram(mensaje)

    guardar_estado(actual)

    resultado = {
        "status": "ok",
        "nuevas": len(nuevas),
        "bajaron": len(precio_baja),
        "subieron": len(precio_sube),
        "total": len(actual)
    }
    log.info(
        f"Listo. Nuevas: {len(nuevas)} | Bajaron: {len(precio_baja)} | Subieron: {len(precio_sube)}"
    )
    return resultado


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