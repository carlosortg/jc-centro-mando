# Centro de Mando JC 🛰️🇻🇪

Monitoreo de flota para las apps **AxiaConnect** en calle. Cada despliegue de
cliente (main.py) hace *phone-home* a este servicio cada N minutos; el panel
muestra en un solo lugar: **online/offline, versión, estado de BD y KPIs del día**
(ventas, facturas, pedidos) de todos los clientes.

Modelo **push**: como los clientes corren en Windows tras NAT (no alcanzables
desde internet), son ellos quienes reportan hacia este servicio.

Un solo archivo (`app.py`), FastAPI + SQLite, sin dependencias externas. Pensado
para correr como contenedor en **Coolify** con un volumen persistente.

---

## 1. Desplegar en Coolify (VPS)

1. Sube este proyecto a un repo Git propio (ver sección 4) y en Coolify crea un
   recurso **Application → desde tu repositorio** (build por **Dockerfile**).
2. **Variables de entorno** (Coolify → Environment Variables), a partir de
   `.env.example`:
   - `INGEST_TOKEN` — token largo y secreto (`openssl rand -hex 24`). Es el que
     validará los heartbeats. Guárdalo: va también en cada cliente.
   - `ADMIN_USER` / `ADMIN_PASSWORD` — acceso al panel.
   - `OFFLINE_AFTER_MIN` — default 45.
   - `DATA_DIR=/data`
3. **Volumen persistente**: monta uno en `/data` (ahí vive el SQLite; sin esto se
   pierde el historial al redeploy).
4. **Puerto**: el contenedor expone `8090`. Coolify le pone su dominio HTTPS.
5. **Healthcheck**: `/health` (ya definido en el Dockerfile).

Al terminar tendrás una URL pública, p.ej. `https://monitor.jcsystems.com`.

## 2. Apuntar cada cliente al centro de mando

En el `.env` de cada despliegue de AxiaConnect (main.py) agrega:

```
JC_MONITOR_URL=https://monitor.jcsystems.com/api/heartbeat
JC_MONITOR_TOKEN=<el mismo INGEST_TOKEN de arriba>
JC_CLIENTE_NOMBRE=Farmacia La Salud
JC_MONITOR_INTERVAL_MIN=15
```

Reinicia la API del cliente. A los pocos minutos aparece en el panel.
Sin `JC_MONITOR_URL`, el cliente no reporta nada (opt-in).

## 3. Usar el panel

Abre la URL pública, entra con `ADMIN_USER`/`ADMIN_PASSWORD`. El panel se
autorefresca cada 30s:
- **● online / ● offline** — según el último heartbeat vs `OFFLINE_AFTER_MIN`.
- **Versión** — resaltada en ámbar si el cliente está desactualizado respecto a
  la última versión vista en la flota.
- **BD** — `ok` o el error reportado.
- **Ventas hoy / Facturas / Pedidos** — KPIs del día de ese cliente.

## 4. Probar localmente (opcional)

```bash
pip install -r requirements.txt
INGEST_TOKEN=dev ADMIN_PASSWORD=dev python app.py
# Panel:  http://127.0.0.1:8090  (admin / dev)
# Simular un heartbeat:
curl -X POST http://127.0.0.1:8090/api/heartbeat \
  -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  -d '{"deployment_id":"test-1","cliente":"Farmacia Demo","empresa_id":"001000","app_version":"26.0.0","db_status":"ok","ventas_dia":15230,"num_facturas":12,"num_pedidos":3}'
```

## 5. Subir a GitHub (repo propio, separado de AxiaConnect-API)

```bash
cd jc-centro-mando
git init && git add . && git commit -m "init: centro de mando JC"
gh repo create jc-centro-mando --private --source=. --push
```

---

## Notas de arquitectura

- **SQLite** es suficiente para una flota de decenas de clientes. Si algún día
  la flota crece mucho o quieres histórico/gráficas, se migra a Postgres (Coolify
  lo ofrece como recurso) cambiando solo la capa de datos de `app.py`.
- **Seguridad**: la ingesta exige `Bearer INGEST_TOKEN`; el panel exige Basic Auth.
  Nunca comitees `.env` (está en `.gitignore`). Sirve siempre por HTTPS (Coolify).
- **Privacidad**: el heartbeat incluye KPIs de negocio del cliente (ventas del
  día). Debe estar consentido con cada cliente. Si algún cliente no lo acepta, se
  puede dejar `JC_CLIENTE_NOMBRE` y omitir KPIs desde su `main.py` (futuro flag).
- Este servicio es la parte **WS4b** del plan `main-design-20260712-mejoras`. El
  emisor (WS4a) ya vive en `main.py` de AxiaConnect-API.
