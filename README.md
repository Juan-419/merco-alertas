# 🛒 MercoApp Alertas — Bot de ofertas semanales

Bot que revisa automáticamente las ofertas de [mercoapp.com](https://mercoapp.com/categoria-producto/ofertas/) cada **sábado a las 10:00 a.m. (hora Bogotá)** y te notifica por Telegram con foto, precio y link de cada producto.

---

## ¿Qué detecta?

- 🆕 **Nuevas ofertas** — productos que no estaban la semana pasada
- 📉 **Precios que bajaron** — con variación porcentual
- 📈 **Precios que subieron** — para que no te sorprenda

Cada producto llega como mensaje individual con su foto, nombre, precio anterior/actual y link directo.

---

## 🚀 Deploy en Render

### 1. Sube el código a GitHub

```bash
git init
git add .
git commit -m "init: merco-alertas bot"
git remote add origin https://github.com/TU_USUARIO/merco-alertas.git
git push -u origin main
```

### 2. Crea el servicio en Render

1. Ve a [render.com](https://render.com) → **New → Web Service**
2. Conecta tu repositorio de GitHub
3. Render detecta el `render.yaml` automáticamente

### 3. Variables de entorno

En el dashboard de Render → **Environment**, agrega:

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token de tu bot (lo da @BotFather) |
| `TELEGRAM_CHAT_ID` | Tu chat ID de Telegram |

### 4. Cómo obtener el token y chat ID de Telegram

**Token:**
1. Abre Telegram y busca **@BotFather**
2. Escribe `/newbot` y sigue los pasos
3. Te da un token tipo `7123456789:AAF...`

**Chat ID:**
1. Escríbele un mensaje a tu bot
2. Entra a `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
3. Busca el campo `"id"` dentro de `"chat"`

---

## 🧪 Endpoints

| Endpoint | Descripción |
|---|---|
| `/` | Estado general del bot |
| `/revisar-ahora` | Fuerza una revisión inmediata |
| `/estado` | Lista de ofertas en caché |
| `/healthz` | Health check |

Una vez desplegado, prueba entrando a:
```
https://tu-app.onrender.com/revisar-ahora
```

---

## ⏰ UptimeRobot (importante)

Render en plan gratuito duerme el servicio si no recibe tráfico por 15 minutos, lo que haría que el scheduler del sábado no funcione.

**Solución gratis:**
1. Crea cuenta en [uptimerobot.com](https://uptimerobot.com)
2. New Monitor → HTTP(s)
3. URL: `https://tu-app.onrender.com/healthz`
4. Intervalo: **cada 5 minutos**

Listo, el bot siempre estará despierto.

---

## 📦 Stack

- **Flask** — servidor web
- **Playwright + Chromium** — scraping (bypasea Cloudflare)
- **Telegram Bot API** — notificaciones con foto
- **Threading** — scheduler semanal sin dependencias externas
- **Gunicorn** — servidor de producción