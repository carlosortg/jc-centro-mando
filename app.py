"""
Centro de Mando JC — Receptor de heartbeats (monitoreo de flota AxiaConnect).

Modelo push: cada despliegue de AxiaConnect (main.py) hace phone-home a este
servicio cada N minutos. Este receptor guarda el último estado de cada cliente
y lo muestra en un panel: online/offline, versión, estado de BD y KPIs del día.

Un solo archivo, SQLite, sin dependencias de infraestructura externa: pensado
para correr como un contenedor en Coolify con un volumen persistente en /data.

Auth:
  - Ingesta (POST /api/heartbeat): header  Authorization: Bearer <INGEST_TOKEN>
    (debe coincidir con JC_MONITOR_TOKEN de cada cliente).
  - Panel y API de lectura: HTTP Basic Auth (ADMIN_USER / ADMIN_PASSWORD).

Variables de entorno:
  INGEST_TOKEN      token que valida los heartbeats entrantes (obligatorio en prod)
  ADMIN_USER        usuario del panel               (default: admin)
  ADMIN_PASSWORD    contraseña del panel            (obligatorio en prod)
  DATA_DIR          carpeta del SQLite              (default: ./data ; en Coolify: /data)
  OFFLINE_AFTER_MIN minutos sin heartbeat = offline (default: 45)
  PORT              puerto HTTP                      (default: 8090)
"""
import os
import sqlite3
import secrets
from typing import Optional
from datetime import datetime, timezone
from contextlib import closing

from fastapi import FastAPI, Depends, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()
ADMIN_USER = os.getenv("ADMIN_USER", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
DATA_DIR = os.getenv("DATA_DIR", "./data").strip()
OFFLINE_AFTER_MIN = int(os.getenv("OFFLINE_AFTER_MIN", "45"))
PORT = int(os.getenv("PORT", "8090"))

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "centro_mando.db")

app = FastAPI(title="Centro de Mando JC", version="1.0.0")
security = HTTPBasic()


# ── Base de datos ─────────────────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deployments (
                deployment_id TEXT PRIMARY KEY,
                cliente       TEXT,
                empresa_id    TEXT,
                app_version   TEXT,
                api_version   TEXT,
                db_status     TEXT,
                hostname      TEXT,
                ventas_dia    REAL,
                num_facturas  INTEGER,
                num_pedidos   INTEGER,
                first_seen    TEXT,
                last_seen     TEXT
            )
        """)
        conn.commit()


# ── Auth ──────────────────────────────────────────────────────────────────────
def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Panel/API de lectura tras Basic Auth. Comparación en tiempo constante."""
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
    pass_ok = bool(ADMIN_PASSWORD) and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="No autorizado",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def require_ingest_token(authorization: str = Header(default="")):
    """Valida el Bearer token de los heartbeats entrantes."""
    if not INGEST_TOKEN:
        # Sin token configurado no se acepta ingesta (evita panel abierto por error).
        raise HTTPException(status_code=503, detail="Ingesta no configurada (falta INGEST_TOKEN).")
    expected = f"Bearer {INGEST_TOKEN}"
    if not secrets.compare_digest(authorization.strip(), expected):
        raise HTTPException(status_code=401, detail="Token inválido")
    return True


# ── Modelo del heartbeat (debe coincidir con el emisor de main.py) ────────────
class Heartbeat(BaseModel):
    deployment_id: str
    cliente: str = ""
    empresa_id: str = ""
    app_version: str = ""
    api_version: str = ""
    db_status: str = ""
    hostname: str = ""
    ts: str = ""
    ventas_dia: Optional[float] = None
    num_facturas: Optional[int] = None
    num_pedidos: Optional[int] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/heartbeat")
def receive_heartbeat(hb: Heartbeat, _: bool = Depends(require_ingest_token)):
    if not hb.deployment_id.strip():
        raise HTTPException(status_code=400, detail="deployment_id requerido")
    now = datetime.now(timezone.utc).isoformat()
    with closing(_db()) as conn:
        conn.execute("""
            INSERT INTO deployments
                (deployment_id, cliente, empresa_id, app_version, api_version,
                 db_status, hostname, ventas_dia, num_facturas, num_pedidos,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deployment_id) DO UPDATE SET
                cliente=excluded.cliente,
                empresa_id=excluded.empresa_id,
                app_version=excluded.app_version,
                api_version=excluded.api_version,
                db_status=excluded.db_status,
                hostname=excluded.hostname,
                ventas_dia=excluded.ventas_dia,
                num_facturas=excluded.num_facturas,
                num_pedidos=excluded.num_pedidos,
                last_seen=excluded.last_seen
        """, (hb.deployment_id.strip(), hb.cliente, hb.empresa_id, hb.app_version,
              hb.api_version, hb.db_status, hb.hostname, hb.ventas_dia,
              hb.num_facturas, hb.num_pedidos, now, now))
        conn.commit()
    return {"status": "ok"}


def _version_tuple(v: str):
    """Convierte 'X.Y.Z' en tupla comparable; tolera basura."""
    parts = []
    for p in (v or "").split("."):
        try:
            parts.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except Exception:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


@app.get("/api/deployments")
def list_deployments(_: str = Depends(require_admin)):
    with closing(_db()) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM deployments ORDER BY cliente COLLATE NOCASE").fetchall()]
    now = datetime.now(timezone.utc)
    latest = None
    for r in rows:
        vt = _version_tuple(r.get("app_version"))
        if latest is None or vt > latest:
            latest = vt
    online_count = 0
    for r in rows:
        # minutos desde el último heartbeat
        mins = None
        try:
            ls = datetime.fromisoformat(r["last_seen"])
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            mins = (now - ls).total_seconds() / 60.0
        except Exception:
            mins = None
        r["mins_since"] = round(mins, 1) if mins is not None else None
        r["online"] = (mins is not None and mins <= OFFLINE_AFTER_MIN)
        r["outdated"] = (latest is not None and _version_tuple(r.get("app_version")) < latest)
        if r["online"]:
            online_count += 1
    return JSONResponse({
        "generated_at": now.isoformat(),
        "offline_after_min": OFFLINE_AFTER_MIN,
        "total": len(rows),
        "online": online_count,
        "offline": len(rows) - online_count,
        "latest_version": ".".join(str(x) for x in latest) if latest else "",
        "deployments": rows,
    })


@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(require_admin)):
    return HTMLResponse(_DASHBOARD_HTML)


# ── Panel (HTML + JS, auto-refresca cada 30s) ─────────────────────────────────
_DASHBOARD_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Centro de Mando JC</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --border:#334155; --txt:#e2e8f0; --mut:#94a3b8;
          --ok:#10b981; --off:#ef4444; --warn:#f59e0b; --pri:#6366f1; }
  * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--txt);
    font-family:system-ui,Segoe UI,Roboto,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--border); display:flex;
    align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
  h1 { font-size:18px; margin:0; font-weight:800; letter-spacing:-.3px; }
  .flag { font-size:16px; }
  .stats { display:flex; gap:18px; font-size:13px; color:var(--mut); flex-wrap:wrap; }
  .stats b { color:var(--txt); }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  main { padding:20px 24px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); white-space:nowrap; }
  th { color:var(--mut); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }
  tr:hover td { background:rgba(255,255,255,.02); }
  .cliente { font-weight:700; }
  .badge { padding:2px 8px; border-radius:6px; font-size:11px; font-weight:700; }
  .b-ok { background:rgba(16,185,129,.15); color:var(--ok); }
  .b-off { background:rgba(239,68,68,.15); color:var(--off); }
  .b-warn { background:rgba(245,158,11,.15); color:var(--warn); }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .mut { color:var(--mut); }
  .empty { padding:60px; text-align:center; color:var(--mut); }
  footer { padding:14px 24px; color:var(--mut); font-size:11px; border-top:1px solid var(--border); }
</style></head><body>
<header>
  <div><h1>🛰️ Centro de Mando JC <span class="flag">🇻🇪</span></h1>
    <div class="mut" style="font-size:11px;margin-top:2px;">Monitoreo de flota AxiaConnect</div></div>
  <div class="stats">
    <span><span class="dot" style="background:var(--ok)"></span><b id="s-online">–</b> online</span>
    <span><span class="dot" style="background:var(--off)"></span><b id="s-offline">–</b> offline</span>
    <span>Total: <b id="s-total">–</b></span>
    <span>Última versión: <b id="s-latest">–</b></span>
    <span class="mut" id="s-updated">—</span>
  </div>
</header>
<main>
  <table id="tbl">
    <thead><tr>
      <th>Estado</th><th>Cliente</th><th>Empresa</th><th>Versión</th><th>BD</th>
      <th class="num">Ventas hoy</th><th class="num">Facturas</th><th class="num">Pedidos</th>
      <th>Host</th><th>Últ. reporte</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="10" class="empty">Cargando…</td></tr></tbody>
  </table>
</main>
<footer>Se actualiza cada 30s · offline = sin reportar en <span id="f-off">–</span> min · JC Systems / Axia Core de Venezuela</footer>
<script>
function fmtMoney(n){ if(n==null) return '—'; return 'Bs ' + Number(n).toLocaleString('es-VE',{maximumFractionDigits:0}); }
function fmtMins(m){ if(m==null) return '—'; if(m<1) return 'hace segundos'; if(m<60) return 'hace '+Math.round(m)+' min';
  const h=Math.floor(m/60); if(h<24) return 'hace '+h+' h'; return 'hace '+Math.floor(h/24)+' d'; }
async function load(){
  try{
    const r = await fetch('/api/deployments', {cache:'no-store'});
    if(!r.ok){ document.getElementById('rows').innerHTML='<tr><td colspan="10" class="empty">Error '+r.status+'</td></tr>'; return; }
    const d = await r.json();
    document.getElementById('s-online').textContent = d.online;
    document.getElementById('s-offline').textContent = d.offline;
    document.getElementById('s-total').textContent = d.total;
    document.getElementById('s-latest').textContent = d.latest_version || '—';
    document.getElementById('f-off').textContent = d.offline_after_min;
    document.getElementById('s-updated').textContent = 'act. ' + new Date(d.generated_at).toLocaleTimeString('es-VE');
    const tb = document.getElementById('rows');
    if(!d.deployments.length){ tb.innerHTML='<tr><td colspan="10" class="empty">Aún no hay clientes reportando. Configura JC_MONITOR_URL en cada despliegue.</td></tr>'; return; }
    tb.innerHTML = d.deployments.map(x=>{
      const est = x.online ? '<span class="badge b-ok">● online</span>' : '<span class="badge b-off">● offline</span>';
      const dbok = (x.db_status==='ok');
      const db = '<span class="badge '+(dbok?'b-ok':'b-off')+'">'+(x.db_status||'—')+'</span>';
      const ver = '<span class="badge '+(x.outdated?'b-warn':'b-ok')+'">v'+(x.app_version||'?')+'</span>';
      return '<tr>'
        + '<td>'+est+'</td>'
        + '<td class="cliente">'+(x.cliente||'(sin nombre)')+'</td>'
        + '<td class="mut">'+(x.empresa_id||'—')+'</td>'
        + '<td>'+ver+'</td>'
        + '<td>'+db+'</td>'
        + '<td class="num">'+fmtMoney(x.ventas_dia)+'</td>'
        + '<td class="num">'+(x.num_facturas??'—')+'</td>'
        + '<td class="num">'+(x.num_pedidos??'—')+'</td>'
        + '<td class="mut">'+(x.hostname||'—')+'</td>'
        + '<td class="mut">'+fmtMins(x.mins_since)+'</td>'
        + '</tr>';
    }).join('');
  }catch(e){ console.error(e); }
}
load(); setInterval(load, 30000);
</script>
</body></html>"""


@app.on_event("startup")
def _startup():
    init_db()


if __name__ == "__main__":
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
