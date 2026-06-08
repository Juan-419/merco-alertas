import os
import json
import re
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from playwright.sync_api import sync_playwright
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config desde variables de entorno ─────────────────────────────────────────
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
RECIPIENT      = os.environ["RECIPIENT_EMAIL"]
OFERTAS_URL    = "https://mercoapp.com/categoria-producto/ofertas/"
STATE_FILE     = "ofertas_state.json"


# ── Utilidades de precio ───────────────────────────────────────────────────────

def parsear_precio(texto: str | None) -> float | None:
    """Convierte '$ 44.504' o '$44.504,50' a float. Retorna None si falla."""
    if not texto:
        return None
    # Quitar símbolo de moneda, espacios y puntos de miles; reemplazar coma decimal
    limpio = re.sub(r"[^\d,.]", "", texto)
    # Formato colombiano: puntos = miles, coma = decimal  →  44.504 → 44504
    limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None

def variacion_pct(antes: float, despues: float) -> str:
    """Devuelve '+12.3 %' o '-8.5 %'."""
    if antes == 0:
        return ""
    pct = ((despues - antes) / antes) * 100
    signo = "+" if pct > 0 else ""
    return f"{signo}{pct:.1f} %"


# ── Persistencia ──────────────────────────────────────────────────────────────

def cargar_estado() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def guardar_estado(ofertas: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(ofertas, f, ensure_ascii=False, indent=2)


# ── Scraping ──────────────────────────────────────────────────────────────────

def scrapear_ofertas() -> dict:
    """
    Usa Playwright (Chromium headless) para bypassear Cloudflare.
    Retorna dict  nombre → {precio_nuevo, precio_original, precio_display, link, imagen}
    """
    productos = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.6367.82 Mobile Safari/537.36"
                ),
                locale="es-CO",
                viewport={"width": 390, "height": 844},
            )
            page = context.new_page()

            page.goto("https://mercoapp.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            page.goto(OFERTAS_URL, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)

            items = page.query_selector_all("li.product, article.product")
            log.info(f"Elementos producto encontrados: {len(items)}")

            for item in items:
                nombre_tag   = item.query_selector(".woocommerce-loop-product__title, h2, h3")
                precio_ins   = item.query_selector("ins .woocommerce-Price-amount, ins bdi")
                precio_del   = item.query_selector("del .woocommerce-Price-amount, del bdi")
                # Precio único (sin oferta explícita)
                precio_unico = item.query_selector(".woocommerce-Price-amount:not(ins *):not(del *), bdi:not(ins *):not(del *)")
                link_tag     = item.query_selector("a.woocommerce-loop-product__link, a")
                img_tag      = item.query_selector("img")

                if not nombre_tag:
                    continue

                nombre = nombre_tag.inner_text().strip()

                # precio_nuevo = precio con descuento (dentro de <ins>)
                # precio_original = precio antes del descuento (dentro de <del>)
                # precio_display = lo que se muestra al usuario (para comparar cambios)
                precio_nuevo_txt    = precio_ins.inner_text().strip()   if precio_ins   else None
                precio_original_txt = precio_del.inner_text().strip()   if precio_del   else None
                precio_unico_txt    = precio_unico.inner_text().strip() if precio_unico else None

                # El "precio vigente" que usamos para detectar cambios
                precio_display = precio_nuevo_txt or precio_unico_txt

                productos[nombre] = {
                    "precio_nuevo":    precio_nuevo_txt,
                    "precio_original": precio_original_txt,
                    "precio_display":  precio_display,   # precio que el cliente paga hoy
                    "link":  link_tag.get_attribute("href") if link_tag else OFERTAS_URL,
                    "imagen": (
                        img_tag.get_attribute("src") or img_tag.get_attribute("data-src", "")
                        if img_tag else ""
                    ),
                }

            browser.close()
    except Exception as e:
        log.error(f"Error en scraping con Playwright: {e}")

    log.info(f"Ofertas scrapeadas: {len(productos)}")
    return productos


# ── Comparación de estados ────────────────────────────────────────────────────

def detectar_cambios(anterior: dict, actual: dict) -> tuple[dict, dict, dict]:
    """
    Compara dos estados y retorna:
      nuevas        – productos que no estaban antes
      precio_baja   – productos cuyo precio bajó  (incluye nuevos descuentos)
      precio_sube   – productos cuyo precio subió
    Cada entrada: nombre → {antes, despues, variacion, ...datos actuales}
    """
    nuevas      = {}
    precio_baja = {}
    precio_sube = {}

    for nombre, datos in actual.items():
        if nombre not in anterior:
            nuevas[nombre] = datos
            continue

        datos_ant = anterior[nombre]
        val_antes  = parsear_precio(datos_ant.get("precio_display"))
        val_despues = parsear_precio(datos.get("precio_display"))

        if val_antes is None or val_despues is None:
            continue

        if val_despues < val_antes:
            precio_baja[nombre] = {
                **datos,
                "precio_antes_txt": datos_ant.get("precio_display"),
                "precio_ahora_txt": datos.get("precio_display"),
                "variacion": variacion_pct(val_antes, val_despues),
            }
        elif val_despues > val_antes:
            precio_sube[nombre] = {
                **datos,
                "precio_antes_txt": datos_ant.get("precio_display"),
                "precio_ahora_txt": datos.get("precio_display"),
                "variacion": variacion_pct(val_antes, val_despues),
            }

    return nuevas, precio_baja, precio_sube


# ── Construcción del correo ───────────────────────────────────────────────────

def _tabla(filas_html: str, columnas: list[str]) -> str:
    ths = "".join(
        f'<th style="padding:10px 8px;text-align:left;">{c}</th>' for c in columnas
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
    </table>"""

def _fila_nueva(nombre: str, d: dict) -> str:
    link = d.get("link", OFERTAS_URL)
    return f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:11px 8px;font-weight:600;color:#1a1a1a;">{nombre}</td>
      <td style="padding:11px 8px;color:#888;text-decoration:line-through;">{d.get('precio_original') or '—'}</td>
      <td style="padding:11px 8px;color:#e53935;font-weight:700;">{d.get('precio_nuevo') or d.get('precio_display') or '—'}</td>
      <td style="padding:11px 8px;">
        <a href="{link}" style="background:#e53935;color:#fff;padding:5px 12px;
           border-radius:6px;text-decoration:none;font-size:13px;">Ver →</a>
      </td>
    </tr>"""

def _fila_cambio(nombre: str, d: dict, es_baja: bool) -> str:
    color    = "#2e7d32" if es_baja else "#b71c1c"
    icono    = "📉" if es_baja else "📈"
    link     = d.get("link", OFERTAS_URL)
    variacion = d.get("variacion", "")
    return f"""
    <tr style="border-bottom:1px solid #f0f0f0;">
      <td style="padding:11px 8px;font-weight:600;color:#1a1a1a;">{nombre}</td>
      <td style="padding:11px 8px;color:#888;">{d.get('precio_antes_txt') or '—'}</td>
      <td style="padding:11px 8px;color:{color};font-weight:700;">{d.get('precio_ahora_txt') or '—'}</td>
      <td style="padding:11px 8px;color:{color};font-weight:600;">{icono} {variacion}</td>
      <td style="padding:11px 8px;">
        <a href="{link}" style="background:{color};color:#fff;padding:5px 12px;
           border-radius:6px;text-decoration:none;font-size:13px;">Ver →</a>
      </td>
    </tr>"""

def construir_seccion(titulo: str, icono: str, color: str, items: dict,
                      tipo: str) -> str:
    """tipo: 'nueva' | 'baja' | 'sube'"""
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
      <span style="font-size:14px;font-weight:400;color:#888;">({len(items)} producto{'s' if len(items)!=1 else ''})</span>
    </h2>
    {tabla}"""

def construir_email_html(nuevas: dict, precio_baja: dict,
                          precio_sube: dict, todas: dict) -> str:
    fecha = datetime.now().strftime("%d de %B de %Y, %I:%M %p")

    total_cambios = len(nuevas) + len(precio_baja) + len(precio_sube)
    if total_cambios == 0:
        resumen_badge = "Sin novedades esta semana 😴"
    else:
        partes = []
        if nuevas:      partes.append(f"{len(nuevas)} nueva{'s' if len(nuevas)!=1 else ''}")
        if precio_baja: partes.append(f"{len(precio_baja)} bajaron de precio")
        if precio_sube: partes.append(f"{len(precio_sube)} subieron de precio")
        resumen_badge = " · ".join(partes)

    sec_nuevas = construir_seccion(
        "Nuevas ofertas", "🆕", "#e53935", nuevas, "nueva")
    sec_baja = construir_seccion(
        "Precios que bajaron", "📉", "#2e7d32", precio_baja, "baja")
    sec_sube = construir_seccion(
        "Precios que subieron", "📈", "#b71c1c", precio_sube, "sube")

    sin_novedades = (
        "<p style='color:#aaa;font-size:14px;margin-top:24px;'>"
        "No hubo cambios en los productos de la sección Ofertas esta semana.</p>"
        if total_cambios == 0 else ""
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
<table width="100%" bgcolor="#f5f5f5" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:30px 10px;">
<table width="640" style="background:#fff;border-radius:12px;overflow:hidden;
       box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:100%;">

  <!-- Header -->
  <tr><td style="background:#e53935;padding:28px 32px;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:22px;">🛒 MercoApp · Alerta de Ofertas</h1>
    <p style="color:rgba(255,255,255,.85);margin:6px 0 0;font-size:13px;">{fecha}</p>
  </td></tr>

  <!-- Resumen rápido -->
  <tr><td style="background:#fff8f8;padding:16px 32px;border-bottom:1px solid #fdecea;">
    <p style="margin:0;color:#555;font-size:14px;">
      📦 Total en oferta: <strong>{len(todas)}</strong> &nbsp;·&nbsp; {resumen_badge}
    </p>
  </td></tr>

  <!-- Cuerpo -->
  <tr><td style="padding:24px 32px;">
    {sec_nuevas}
    {sec_baja}
    {sec_sube}
    {sin_novedades}

    <div style="margin-top:36px;text-align:center;">
      <a href="{OFERTAS_URL}"
         style="background:#e53935;color:#fff;padding:12px 28px;border-radius:8px;
                text-decoration:none;font-weight:700;font-size:15px;">
        Ver todas las ofertas →
      </a>
    </div>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#fafafa;padding:16px 32px;text-align:center;
          color:#bbb;font-size:12px;border-top:1px solid #f0f0f0;">
    Bot automático de MercoApp 🤖 · Próxima revisión: el próximo sábado a las 10:00 a.m.
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


# ── Email ─────────────────────────────────────────────────────────────────────

def enviar_email(asunto: str, html: str):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    log.info(f"Email enviado a {RECIPIENT}")


# ── Job principal ─────────────────────────────────────────────────────────────

def revisar_ofertas():
    log.info("=== Revisión semanal MercoApp iniciada ===")
    anterior = cargar_estado()
    actual   = scrapear_ofertas()

    if not actual:
        log.warning("Sin ofertas. Se omite el envío.")
        return

    nuevas, precio_baja, precio_sube = detectar_cambios(anterior, actual)
    guardar_estado(actual)

    total_cambios = len(nuevas) + len(precio_baja) + len(precio_sube)

    # Asunto dinámico según novedades
    partes_asunto = []
    if nuevas:      partes_asunto.append(f"{len(nuevas)} nueva{'s' if len(nuevas)!=1 else ''}")
    if precio_baja: partes_asunto.append(f"{len(precio_baja)} precio{'s' if len(precio_baja)!=1 else ''} bajaron 📉")
    if precio_sube: partes_asunto.append(f"{len(precio_sube)} precio{'s' if len(precio_sube)!=1 else ''} subieron 📈")

    asunto = (
        "🛒 MercoApp · " + " | ".join(partes_asunto)
        if partes_asunto else
        "🛒 MercoApp · Sin novedades esta semana"
    )

    html = construir_email_html(nuevas, precio_baja, precio_sube, actual)
    enviar_email(asunto, html)
    log.info(
        f"Listo. Nuevas: {len(nuevas)} | Bajaron: {len(precio_baja)} "
        f"| Subieron: {len(precio_sube)} | Total: {len(actual)}"
    )


# ── Scheduler (sábados 10:00 a.m. Bogotá) ────────────────────────────────────

scheduler = BackgroundScheduler(timezone="America/Bogota")
scheduler.add_job(
    revisar_ofertas,
    trigger="cron",
    day_of_week="sat",
    hour=10,
    minute=0,
    id="revisar_ofertas_sabado",
)
scheduler.start()
log.info("Scheduler activo → cada sábado 10:00 a.m. (Bogotá)")


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "status": "activo",
        "descripcion": "Bot de alertas de ofertas MercoApp",
        "proxima_revision": "Cada sábado 10:00 a.m. (hora Bogotá)",
        "detecta": ["nuevas ofertas", "bajadas de precio", "subidas de precio"],
        "ofertas_en_cache": len(cargar_estado()),
        "url_monitorizada": OFERTAS_URL,
    })

@app.route("/revisar-ahora")
def revisar_ahora():
    """Fuerza una revisión manual — útil para testear."""
    try:
        revisar_ofertas()
        return jsonify({"status": "ok", "mensaje": "Revisión ejecutada. Revisa tu correo."})
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

@app.route("/estado")
def estado():
    ofertas = cargar_estado()
    return jsonify({"total": len(ofertas), "ofertas": ofertas})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)