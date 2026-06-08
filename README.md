# 🛒 MercoApp Alertas — Bot de ofertas semanales

Bot que revisa automáticamente las ofertas de [mercoapp.com](https://mercoapp.com/categoria-producto/ofertas/) cada **sábado a las 10:00 a.m. (hora Bogotá)** y te notifica por correo si hay productos nuevos en descuento.

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

### 3. Configura las variables de entorno

En el dashboard de Render → **Environment**, agrega:

| Variable          | Valor                                      |
|-------------------|--------------------------------------------|
| `GMAIL_USER`      | `tucorreo@gmail.com`                       |
| `GMAIL_PASSWORD`  | App Password de 16 caracteres (sin espacios) |
| `RECIPIENT_EMAIL` | correo donde quieres recibir las alertas   |

### 4. Obtener el App Password de Gmail

1. Ve a tu cuenta Google → **Seguridad**
2. Activa **Verificación en dos pasos** (si no la tienes)
3. Busca **"Contraseñas de aplicación"**
4. Crea una nueva → selecciona "Correo" y "Windows" → copia las 16 letras

---

## 🧪 Probar manualmente

Una vez desplegado, visita:

```
https://tu-app.onrender.com/revisar-ahora
```

Esto fuerza una revisión inmediata y te envía el correo. Perfecto para verificar que todo funciona.

### Otros endpoints

| Endpoint        | Descripción                              |
|-----------------|------------------------------------------|
| `/`             | Estado general del bot                   |
| `/estado`       | Lista de ofertas actualmente en caché    |
| `/revisar-ahora`| Fuerza revisión inmediata + envío de email |

---

## ⚠️ Nota sobre el plan Free de Render

Render en plan gratuito **duerme el servicio** si no recibe tráfico por 15 minutos. Para que el scheduler funcione correctamente, se recomienda usar [UptimeRobot](https://uptimerobot.com) (gratis) para hacer ping a `/` cada 10 minutos.

---

## 📦 Stack

- **Flask** — servidor web
- **APScheduler** — programación del job semanal
- **BeautifulSoup4** — scraping de MercoApp
- **Gmail SMTP** — envío de correos
- **Gunicorn** — servidor de producción
