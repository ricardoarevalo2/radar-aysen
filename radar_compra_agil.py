#!/usr/bin/env python3
"""
RADAR COMPRA ÁGIL — Región de Aysén, filtrado por los rubros inscritos.  (v2)

Uso en el Mac:   python3 radar_compra_agil.py          (genera reportes/radar_aysen.html)
                 python3 radar_compra_agil.py --abrir  (y lo abre en el navegador)
En GitHub:       lo corre .github/workflows/radar.yml  (genera docs/index.html = página web)

Cómo funciona:
  1. Pide a la API de Compra Ágil (api2.mercadopublico.cl) todas las compras
     PUBLICADAS (abiertas) de la región 11 (Aysén).
  2. Para cada compra que aún no conoce, pide su detalle y mira los códigos de producto.
     (La API no filtra por rubro, así que el filtro se hace aquí.)
  3. Producto dentro de tus rubros → OPORTUNIDAD.
     Texto con palabras de tu giro → POSIBLE (revisar a mano).
  4. Genera el reporte HTML y un CSV.

El detalle de cada compra se guarda en cache_compras.json: cada corrida solo consulta
las compras nuevas o las que fallaron la vez anterior.
"""

import csv
import html
import json
import os
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

CARPETA = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CARPETA)

# El ticket viene de una variable de entorno (GitHub) o de config.py (Mac).
TICKET = os.environ.get("COMPRA_AGIL_TICKET", "").strip()
if not TICKET:
    try:
        from config import TICKET  # noqa: F401
    except ImportError:
        sys.exit("❌ Falta el ticket: crea config.py con TICKET = \"...\" o define COMPRA_AGIL_TICKET.")

MODO_WEB = os.environ.get("RADAR_MODO") == "web"

# ───────────── CONFIGURACIÓN ─────────────
REGION = 11  # Aysén
ZONA = timezone(timedelta(hours=-3))  # hora de Aysén

# Rubros inscritos (código de clase ONU/UNSPSC de 8 dígitos).
# Una clase termina en "00"; sus productos comparten los primeros 6 dígitos.
RUBROS = {
    "80141600": "Ventas y marketing",
    "82101600": "Publicidad en medios no impresos",
    "82101800": "Agencias de publicidad",
    "82101900": "Inserciones publicitarias en medios",
    "82131600": "Fotógrafos y camarógrafos",
    "83111800": "Servicios de televisión",
    "83111900": "Servicios de radio",
    "83121700": "Comunicación social y de masas",
}

# Palabras/frases para detectar compras mal categorizadas (quedan como "POSIBLE").
# Se buscan como palabra completa (ej: "radio" no calza con "radiología").
PALABRAS = [
    "radio", "radial", "radiales", "radioemisora", "emisora", "televisión", "television",
    "televisivo", "publicidad", "publicitario", "publicitaria", "publicitarios", "publicitarias",
    "difusión", "difusion", "spot", "spots", "avisaje", "aviso", "avisos",
    "campaña comunicacional", "campaña de difusión", "audiovisual", "audiovisuales",
    "producción de video", "video institucional", "cápsula radial", "cápsulas radiales",
    "cápsula audiovisual", "registro fotográfico", "registro audiovisual", "fotógrafo",
    "fotografía", "filmación", "streaming", "transmisión en vivo", "locución", "locutor",
    "jingle", "cuña radial", "medios de comunicación", "comunicacional", "marketing",
    "redes sociales",
]
# Si el texto trae alguna de estas, NO se marca como posible (evita falsos positivos).
EXCLUIR = ["vigilancia", "cámara de seguridad", "cámaras de seguridad", "cctv"]

BASE_URL = "https://api2.mercadopublico.cl"
CACHE = os.path.join(CARPETA, "cache_compras.json")
SALIDA = os.path.join(CARPETA, "docs" if MODO_WEB else "reportes")
PAUSA = 0.4             # segundos entre páginas del listado
HILOS = 4               # detalles consultados en paralelo (la API tarda ~20 s por detalle)
REINTENTOS_FINALES = 2  # rondas extra para las compras que fallaron
HORAS_NUEVA = 24        # una compra se marca NUEVA durante sus primeras 24 h en el radar
# ─────────────────────────────────────────

PREFIJOS = {codigo[:6]: nombre for codigo, nombre in RUBROS.items()}
SESION = requests.Session()
SESION.headers.update({"ticket": TICKET})


def llamar(ruta, params=None, intentos=5):
    """GET a la API con reintentos ante lentitud (500/504) o saturación (429)."""
    for intento in range(1, intentos + 1):
        try:
            r = SESION.get(BASE_URL + ruta, params=params, timeout=90)
        except requests.RequestException as e:
            print(f"   ⚠️  Error de conexión ({e.__class__.__name__}), reintento {intento}/{intentos}...")
            time.sleep(5 * intento)
            continue
        if r.status_code == 200:
            return r.json().get("payload")
        if r.status_code == 401:
            sys.exit("❌ La API rechazó el ticket (401).")
        if r.status_code == 429:
            espera = int(r.headers.get("Retry-After", 60))
            print(f"   ⏳ API saturada (429). Esperando {espera} s...")
            time.sleep(espera)
            continue
        if r.status_code in (500, 502, 503, 504):
            print(f"   ⏳ API lenta ({r.status_code}), reintento {intento}/{intentos}...")
            time.sleep(5 * intento)
            continue
        print(f"   ❌ HTTP {r.status_code}: {r.text[:200]}")
        return None
    return None


def listar_publicadas():
    """Devuelve (compras, completo). completo=False si alguna página no respondió."""
    compras, pagina = [], 1
    while True:
        payload = llamar("/v2/compra-agil", {
            "region": REGION,
            "estado": "publicada",
            "tamano_pagina": 10,
            "numero_pagina": pagina,
        })
        if not payload:
            return compras, False
        items = payload.get("items", [])
        compras.extend(items)
        total_pag = (payload.get("paginacion") or {}).get("total_paginas", 1)
        print(f"   Página {pagina}/{total_pag} — {len(compras)} compras")
        if pagina >= total_pag or not items:
            return compras, True
        pagina += 1
        time.sleep(PAUSA)


def cargar_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar_cache(cache):
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def clasificar(detalle):
    """Devuelve ('OPORTUNIDAD'|'POSIBLE'|None, motivos)."""
    rubros_hit = set()
    for p in detalle.get("productos_solicitados") or []:
        cod = str(p.get("codigo_producto") or "").strip()
        if cod[:6] in PREFIJOS:
            rubros_hit.add(PREFIJOS[cod[:6]])
    if rubros_hit:
        return "OPORTUNIDAD", sorted(rubros_hit)

    texto = " ".join([
        detalle.get("nombre") or "",
        detalle.get("descripcion") or "",
        " ".join((p.get("nombre") or "") + " " + (p.get("descripcion") or "")
                 for p in detalle.get("productos_solicitados") or []),
    ]).lower()
    if any(x in texto for x in EXCLUIR):
        return None, []
    hits = sorted({w for w in PALABRAS if re.search(r"\b" + re.escape(w) + r"\b", texto)})
    if hits:
        return "POSIBLE", hits
    return None, []


def a_local(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZONA)
    except ValueError:
        return None


def pesos(n):
    try:
        return "$" + f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


# ───────────── REPORTE HTML ─────────────
# Diseño: "fiordo" — tinta azul petróleo, blanco glaciar y un acento turquesa de agua de deshielo.
# Tipografías: Bricolage Grotesque (títulos), IBM Plex Sans (texto), IBM Plex Mono (códigos y montos).

CSS = r"""
:root{
  --bg:#EEF3F4; --surface:#FFFFFF; --surface-2:#F6F9FA; --ink:#12222C; --ink-2:#3D505C;
  --muted:#6B7C86; --line:#DCE5E8; --line-2:#C9D5DA;
  --accent:#0B6F7C; --accent-ink:#FFFFFF; --accent-soft:#DDF0F1;
  --crit:#B93A26; --crit-soft:#FBE9E5; --warn:#A8670E; --warn-soft:#FBF0DC;
  --ok:#2C7A57; --ok-soft:#E3F2EA; --pos:#6A4FA3; --pos-soft:#EEEAF7;
  --shadow:0 1px 2px rgba(18,34,44,.05),0 8px 24px -12px rgba(18,34,44,.12);
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0C1419; --surface:#131E25; --surface-2:#172430; --ink:#E4ECEF; --ink-2:#B5C3CA;
  --muted:#84959E; --line:#22313B; --line-2:#2D3E49;
  --accent:#4DB6C2; --accent-ink:#06262B; --accent-soft:#12343A;
  --crit:#F0735E; --crit-soft:#3A1D18; --warn:#E6A94A; --warn-soft:#352812;
  --ok:#5CC592; --ok-soft:#12301F; --pos:#B19BE6; --pos-soft:#251F38;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px -14px rgba(0,0,0,.6);
  color-scheme:dark;}}
:root[data-theme="dark"]{
  --bg:#0C1419; --surface:#131E25; --surface-2:#172430; --ink:#E4ECEF; --ink-2:#B5C3CA;
  --muted:#84959E; --line:#22313B; --line-2:#2D3E49;
  --accent:#4DB6C2; --accent-ink:#06262B; --accent-soft:#12343A;
  --crit:#F0735E; --crit-soft:#3A1D18; --warn:#E6A94A; --warn-soft:#352812;
  --ok:#5CC592; --ok-soft:#12301F; --pos:#B19BE6; --pos-soft:#251F38;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px -14px rgba(0,0,0,.6);
  color-scheme:dark;}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 "IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  padding:0 16px;padding-block:0 56px}
.wrap{max-width:1040px;margin:0 auto}
.mono{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}

/* Cabecera */
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-block:28px 20px}
.brand{display:flex;align-items:center;gap:14px}
.brand svg{flex:none;width:44px;height:44px}
.eyebrow{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
h1{font-family:"Bricolage Grotesque","IBM Plex Sans",sans-serif;font-weight:700;font-size:clamp(26px,4.4vw,34px);
  letter-spacing:-.02em;line-height:1.05;margin:2px 0 0;text-wrap:balance}
.status{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink-2);
  background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:6px 12px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 var(--ok);animation:pulse 2.4s infinite}
.pulse.old{background:var(--warn);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 55%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}

/* Resumen */
.summary{display:grid;grid-template-columns:repeat(4,1fr);background:var(--surface);border:1px solid var(--line);
  border-radius:16px;box-shadow:var(--shadow);overflow:hidden}
.stat{padding:16px 18px;border-left:1px solid var(--line);min-width:0}
.stat:first-child{border-left:0}
.stat .v{font-family:"Bricolage Grotesque",sans-serif;font-size:28px;font-weight:700;letter-spacing:-.02em;line-height:1.1;
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat .l{font-size:12.5px;color:var(--muted);margin-top:2px}
.stat.hot .v{color:var(--crit)}
@media(max-width:720px){.summary{grid-template-columns:repeat(2,1fr)}.stat:nth-child(3){border-left:0}
  .stat:nth-child(n+3){border-top:1px solid var(--line)}}

/* Herramientas */
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-block:22px 8px;position:sticky;
  top:env(safe-area-inset-top,0px);z-index:5;padding-block:10px;background:var(--bg)}
.search{flex:1 1 240px;display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:0 12px;min-width:0}
.search svg{flex:none;color:var(--muted)}
.search input{border:0;outline:0;background:transparent;color:var(--ink);font:inherit;padding:10px 0;width:100%}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:3px;gap:2px}
.seg button{border:0;background:transparent;color:var(--ink-2);font:inherit;font-size:13.5px;padding:7px 12px;border-radius:9px;cursor:pointer;white-space:nowrap}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--surface);font-weight:600}
.seg button .c{opacity:.65;margin-left:4px;font-size:12px}
select{font:inherit;font-size:13.5px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:9px 10px}
.toggle{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--ink-2);cursor:pointer;user-select:none}
.toggle input{accent-color:var(--accent);width:16px;height:16px}

/* Grupos por cierre */
.group{margin-top:22px}
.group h2{display:flex;align-items:baseline;gap:10px;font-family:"Bricolage Grotesque",sans-serif;font-size:17px;font-weight:600;
  letter-spacing:-.01em;margin:0 0 10px;color:var(--ink)}
.group h2 .n{font-family:"IBM Plex Mono",monospace;font-size:12.5px;color:var(--muted);font-weight:500}
.group h2::after{content:"";flex:1;height:1px;background:var(--line);align-self:center}
.list{display:grid;gap:10px}

/* Tarjeta */
.item{display:grid;grid-template-columns:112px 1fr auto;background:var(--surface);border:1px solid var(--line);
  border-radius:14px;overflow:hidden;transition:border-color .15s,box-shadow .15s}
.item:hover{border-color:var(--line-2);box-shadow:var(--shadow)}
.item.done{opacity:.5}
.clock{display:flex;flex-direction:column;justify-content:center;gap:2px;padding:14px;background:var(--surface-2);border-right:1px solid var(--line)}
.clock .t{font-family:"IBM Plex Mono",monospace;font-size:22px;font-weight:600;line-height:1;letter-spacing:-.02em}
.clock .u{font-size:11.5px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.clock .d{font-size:12px;color:var(--ink-2);margin-top:6px}
.item.crit .clock{background:var(--crit-soft)} .item.crit .clock .t{color:var(--crit)}
.item.warn .clock{background:var(--warn-soft)} .item.warn .clock .t{color:var(--warn)}
.item.closed .clock .t{color:var(--muted);font-size:15px}
.body{padding:14px 16px;min-width:0}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;font-size:12px}
.code{font-family:"IBM Plex Mono",monospace;color:var(--muted);text-decoration:none}
.code:hover{color:var(--accent)}
.pill{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:2px 7px;border-radius:6px}
.pill.new{background:var(--accent);color:var(--accent-ink)}
.pill.rubro{background:var(--ok-soft);color:var(--ok)}
.pill.pos{background:var(--pos-soft);color:var(--pos)}
.pill.nd{background:var(--warn-soft);color:var(--warn)}
.title{font-weight:600;font-size:15.5px;line-height:1.35;margin:6px 0 3px;color:var(--ink);text-wrap:pretty;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.org{font-size:13px;color:var(--ink-2);overflow-wrap:anywhere}
.org span{color:var(--muted)}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.tag{font-size:12px;border:1px solid var(--line);border-radius:7px;padding:2px 8px;color:var(--ink-2);background:var(--surface)}
.tag.m{border-color:transparent;background:var(--ok-soft);color:var(--ok);font-weight:500}
.item.posible .tag.m{background:var(--pos-soft);color:var(--pos)}
.tag .k{font-family:"IBM Plex Mono",monospace;color:var(--muted);margin-right:5px;font-size:11px}
.side{display:flex;flex-direction:column;align-items:flex-end;justify-content:space-between;gap:10px;padding:14px 16px;text-align:right}
.amt{font-family:"IBM Plex Mono",monospace;font-size:17px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
.bids{font-size:12px;color:var(--muted)}
.bids b{color:var(--ink-2);font-weight:600}
.acts{display:flex;gap:6px}
.btn{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:13px;font-weight:600;border-radius:9px;padding:7px 12px;
  cursor:pointer;text-decoration:none;border:1px solid var(--line-2);background:var(--surface);color:var(--ink);white-space:nowrap}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.icon{padding:7px 9px;color:var(--ink-2)}
.btn.icon[aria-pressed="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
.btn:hover{filter:brightness(1.05)}
@media(max-width:720px){
  .item{grid-template-columns:1fr}
  .clock{flex-direction:row;align-items:baseline;gap:8px;border-right:0;border-bottom:1px solid var(--line);padding:10px 14px}
  .clock .t{font-size:18px}.clock .d{margin:0 0 0 auto}
  .side{flex-direction:row;align-items:center;text-align:left;border-top:1px solid var(--line);padding:10px 14px}
  .side .money{display:flex;flex-direction:column}
}

.empty{background:var(--surface);border:1px dashed var(--line-2);border-radius:14px;padding:28px;text-align:center;color:var(--muted)}
details.nd{margin-top:26px;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:4px 16px}
details.nd summary{cursor:pointer;padding:12px 0;font-size:13.5px;color:var(--ink-2);font-weight:500}
details.nd ul{margin:0 0 14px;padding:0;list-style:none;display:grid;gap:8px}
details.nd li{font-size:13px;display:flex;gap:10px;flex-wrap:wrap}
details.nd li .code{min-width:150px}
footer{margin-top:32px;padding-top:18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted);display:grid;gap:10px}
footer .rub{display:flex;flex-wrap:wrap;gap:6px}
footer .rub span{border:1px solid var(--line);border-radius:7px;padding:2px 8px}
footer .rub .k{font-family:"IBM Plex Mono",monospace;margin-right:5px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = r"""
const D = JSON.parse(document.getElementById('datos').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pesos = n => n ? '$' + Math.round(n).toLocaleString('es-CL') : '—';
const DIAS = ['dom','lun','mar','mié','jue','vie','sáb'];
let estado = {}; try { estado = JSON.parse(localStorage.getItem('radar_estado') || '{}') } catch (e) {}
const guardar = () => { try { localStorage.setItem('radar_estado', JSON.stringify(estado)) } catch (e) {} };
let filtro = 'todas';
try { filtro = localStorage.getItem('radar_filtro') || 'todas' } catch (e) {}

function reloj(iso) {
  const t = Date.parse(iso); if (isNaN(t)) return {t:'—', u:'', cls:'', grupo:'Sin fecha', orden:9};
  const h = (t - Date.now()) / 36e5, d = new Date(t);
  const hoy = new Date(); const man = new Date(); man.setDate(hoy.getDate() + 1);
  const mismo = (a, b) => a.toDateString() === b.toDateString();
  const hora = d.toLocaleTimeString('es-CL', {hour:'2-digit', minute:'2-digit', hour12:false});
  const fecha = mismo(d, hoy) ? 'hoy ' + hora : mismo(d, man) ? 'mañana ' + hora : DIAS[d.getDay()] + ' ' + d.getDate() + '/' + (d.getMonth()+1) + ' · ' + hora;
  if (h < 0) return {t:'Cerrada', u:'', d:fecha, cls:'closed', grupo:'Ya cerradas', orden:5};
  let t2, u;
  if (h < 1) { t2 = Math.max(1, Math.round(h * 60)); u = 'min' }
  else if (h < 48) { t2 = Math.round(h); u = h < 1.5 ? 'hora' : 'horas' }
  else { t2 = Math.round(h / 24); u = 'días' }
  const cls = h < 24 ? 'crit' : h < 48 ? 'warn' : '';
  const grupo = mismo(d, hoy) ? 'Cierran hoy' : mismo(d, man) ? 'Cierran mañana' : h < 24*7 ? 'Próximos 7 días' : 'Más adelante';
  const orden = {'Cierran hoy':1,'Cierran mañana':2,'Próximos 7 días':3,'Más adelante':4}[grupo];
  return {t:t2, u, d:fecha, cls, grupo, orden};
}

const ICON = {
  check:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  x:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  ext:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>'
};

function tarjeta(f) {
  const r = reloj(f.cierre_iso), st = estado[f.codigo];
  const esRubro = f.tipo === 'OPORTUNIDAD';
  const tipo = esRubro ? '<span class="pill rubro">Tu rubro</span>' : '<span class="pill pos">Posible</span>';
  const nd = f.sin_detalle ? '<span class="pill nd" title="Mercado Público no entrega el detalle de esta compra; se evaluó solo por su título">Sin detalle</span>' : '';
  const motivos = f.motivos.map(m => `<span class="tag m">${esc(m)}</span>`).join('');
  const prods = f.productos.slice(0, 3).map(p => `<span class="tag"><span class="k">${esc(p[0])}</span>${esc(p[1])}</span>`).join('')
    + (f.productos.length > 3 ? `<span class="tag">+${f.productos.length - 3} más</span>` : '');
  const cls = ['item', r.cls, esRubro ? 'rubro' : 'posible', st ? 'done' : ''].join(' ');
  return `<article class="${cls}" data-cod="${esc(f.codigo)}">
    <div class="clock"><div class="t">${r.t}</div><div class="u">${r.u}</div><div class="d">${esc(r.d || '')}</div></div>
    <div class="body">
      <div class="meta"><a class="code" href="${esc(f.link)}" target="_blank" rel="noopener">${esc(f.codigo)}</a>${tipo}${f.nueva ? '<span class="pill new">Nueva</span>' : ''}${nd}</div>
      <div class="title" title="${esc(f.nombre)}">${esc(f.nombre)}</div>
      <div class="org">${esc(f.organismo)}${f.unidad ? ' <span>· ' + esc(f.unidad) + '</span>' : ''}</div>
      <div class="tags">${motivos}${prods}</div>
    </div>
    <div class="side">
      <div class="money"><div class="amt">${pesos(f.monto)}</div><div class="bids"><b>${f.ofertas || 0}</b> ${f.ofertas == 1 ? 'oferta' : 'ofertas'} · publ. ${esc(f.publicacion)}</div></div>
      <div class="acts">
        <a class="btn primary" href="${esc(f.link)}" target="_blank" rel="noopener">Ver ficha ${ICON.ext}</a>
        <button class="btn icon" type="button" aria-pressed="${st === 'cotizada'}" title="Marcar como cotizada" aria-label="Marcar como cotizada" data-v="cotizada">${ICON.check}</button>
        <button class="btn icon" type="button" aria-pressed="${st === 'descartada'}" title="Descartar" aria-label="Descartar" data-v="descartada">${ICON.x}</button>
      </div>
    </div>
  </article>`;
}

function pintar() {
  const q = $('#q').value.trim().toLowerCase();
  const ocultar = $('#ocultar').checked, orden = $('#orden').value;
  let items = D.filas.filter(f =>
    (filtro === 'todas' || (filtro === 'rubro' ? f.tipo === 'OPORTUNIDAD' : f.tipo === 'POSIBLE')) &&
    !(ocultar && estado[f.codigo]) &&
    (!q || [f.codigo, f.nombre, f.organismo, f.unidad, ...f.motivos, ...f.productos.flat()].join(' ').toLowerCase().includes(q)));
  document.querySelectorAll('.seg button').forEach(b => b.setAttribute('aria-pressed', b.dataset.f === filtro));
  const out = $('#lista');
  if (!items.length) { out.innerHTML = `<div class="empty">${q ? 'Nada coincide con “' + esc(q) + '”.' : 'No hay compras abiertas en esta vista.'}</div>`; return }
  if (orden === 'cierre') {
    const grupos = {};
    items.forEach(f => { const r = reloj(f.cierre_iso); (grupos[r.grupo] ||= {orden:r.orden, xs:[]}).xs.push(f) });
    out.innerHTML = Object.entries(grupos).sort((a, b) => a[1].orden - b[1].orden).map(([g, v]) =>
      `<section class="group"><h2>${g} <span class="n">${v.xs.length}</span></h2><div class="list">${v.xs.map(tarjeta).join('')}</div></section>`).join('');
  } else {
    items = items.slice().sort(orden === 'monto' ? (a, b) => (b.monto || 0) - (a.monto || 0) : (a, b) => (b.publicacion_iso || '').localeCompare(a.publicacion_iso || ''));
    out.innerHTML = `<section class="group"><h2>${orden === 'monto' ? 'Por monto' : 'Publicadas recientemente'} <span class="n">${items.length}</span></h2><div class="list">${items.map(tarjeta).join('')}</div></section>`;
  }
}

$('#lista').addEventListener('click', e => {
  const b = e.target.closest('button[data-v]'); if (!b) return;
  const cod = b.closest('.item').dataset.cod, v = b.dataset.v;
  if (estado[cod] === v) delete estado[cod]; else estado[cod] = v;
  guardar(); pintar();
});
document.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', () => {
  filtro = b.dataset.f; try { localStorage.setItem('radar_filtro', filtro) } catch (e) {} pintar();
}));
['#q', '#ocultar', '#orden'].forEach(s => $(s).addEventListener(s === '#q' ? 'input' : 'change', pintar));

// Antigüedad de la última revisión
(function () {
  const t = Date.parse(D.actualizado), min = Math.round((Date.now() - t) / 6e4);
  const txt = min < 1 ? 'recién' : min < 60 ? `hace ${min} min` : min < 60 * 24 ? `hace ${Math.round(min / 60)} h` : `hace ${Math.round(min / 1440)} días`;
  $('#hace').textContent = txt;
  if (min > 90) $('.pulse').classList.add('old');
})();
pintar();
setInterval(pintar, 60000);
"""

LOGO = """<svg viewBox="0 0 44 44" aria-hidden="true"><rect width="44" height="44" rx="12" fill="var(--ink)"/>
<path d="M11 29a13 13 0 0 1 22 0" fill="none" stroke="var(--accent)" stroke-width="2.4" stroke-linecap="round"/>
<path d="M15.5 29a8 8 0 0 1 13 0" fill="none" stroke="var(--accent)" stroke-width="2.4" stroke-linecap="round" opacity=".75"/>
<circle cx="22" cy="29" r="2.6" fill="var(--bg)"/><path d="M22 29 31 16" stroke="var(--bg)" stroke-width="2" stroke-linecap="round"/></svg>"""


def generar_html(filas, ahora, total_region, pendientes=()):
    ops = [f for f in filas if f["tipo"] == "OPORTUNIDAD"]
    pos = [f for f in filas if f["tipo"] == "POSIBLE"]
    urgentes = [f for f in filas if f["cierre_dt"] and timedelta(0) <= f["cierre_dt"] - ahora < timedelta(hours=48)]
    monto_ops = sum(int(f["monto"] or 0) for f in ops)
    nuevas = sum(1 for f in filas if f["nueva"])

    datos = {
        "actualizado": ahora.isoformat(),
        "filas": [{k: f[k] for k in ("tipo", "motivos", "nueva", "codigo", "nombre", "organismo", "unidad", "monto",
                                     "cierre_iso", "publicacion", "publicacion_iso", "ofertas", "productos", "link",
                                     "sin_detalle")} for f in filas],
    }
    datos_json = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")

    nd = ""
    if pendientes:
        items = "".join(
            f"<li><a class='code' href='https://buscador.mercadopublico.cl/ficha?code={html.escape(c['codigo'])}' "
            f"target='_blank' rel='noopener'>{html.escape(c['codigo'])}</a>"
            f"<span>{html.escape((c.get('nombre') or '')[:110])}</span>"
            f"<span style='color:var(--muted)'>{html.escape((c.get('institucion') or {}).get('organismo_comprador') or '')}</span></li>"
            for c in pendientes)
        n = len(pendientes)
        nd = (f"<details class='nd'><summary>{n} {'compra abierta' if n == 1 else 'compras abiertas'} sin detalle en Mercado Público "
              f"— se evaluaron solo por su título</summary><ul>{items}</ul></details>")

    rubros = "".join(f"<span><span class='k'>{c}</span>{html.escape(n)}</span>" for c, n in RUBROS.items())
    monto_txt = pesos(monto_ops) if monto_ops else "$0"

    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#12222C">
<title>Radar Compra Ágil Aysén</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="wrap">
<header class="head">
  <div class="brand">{LOGO}<div><div class="eyebrow">Compra Ágil · Región de Aysén</div><h1>Radar de oportunidades</h1></div></div>
  <div class="status"><span class="pulse"></span>Revisado <b id="hace">{ahora:%H:%M}</b> · {ahora:%d-%m %H:%M}</div>
</header>
<section class="summary" aria-label="Resumen">
  <div class="stat"><div class="v">{len(ops)}</div><div class="l">en tus rubros</div></div>
  <div class="stat"><div class="v">{len(pos)}</div><div class="l">posibles por revisar</div></div>
  <div class="stat{' hot' if urgentes else ''}"><div class="v">{len(urgentes)}</div><div class="l">cierran en menos de 48 h</div></div>
  <div class="stat"><div class="v mono" style="font-size:24px">{monto_txt}</div><div class="l">en tus rubros · {nuevas} nuevas hoy</div></div>
</section>
<div class="tools">
  <label class="search"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
    <input id="q" type="search" placeholder="Buscar organismo, producto o código" autocomplete="off"></label>
  <div class="seg" role="group" aria-label="Filtrar">
    <button type="button" data-f="todas">Todas<span class="c">{len(filas)}</span></button>
    <button type="button" data-f="rubro">Tus rubros<span class="c">{len(ops)}</span></button>
    <button type="button" data-f="posible">Posibles<span class="c">{len(pos)}</span></button>
  </div>
  <select id="orden" aria-label="Ordenar">
    <option value="cierre">Por cierre</option><option value="monto">Por monto</option><option value="recientes">Más recientes</option>
  </select>
  <label class="toggle"><input id="ocultar" type="checkbox" checked> Ocultar cotizadas y descartadas</label>
</div>
<main id="lista"></main>
{nd}
<footer>
  <div>{total_region} compras ágiles abiertas en la región al momento de la revisión. Fuente: API Compra Ágil de Mercado Público.
  Se revisa cada hora entre 06:30 y 21:30. Las marcas de cotizada/descartada se guardan solo en este navegador.</div>
  <div class="rub">{rubros}</div>
</footer>
</div>
<script type="application/json" id="datos">{datos_json}</script>
<script>{JS}</script>
</body></html>"""


def main():
    ahora = datetime.now(ZONA)
    print("📡 RADAR COMPRA ÁGIL — Región de Aysén")
    print("1) Buscando compras publicadas...")
    compras, completo = listar_publicadas()
    if not compras:
        sys.exit("No llegaron compras (¿API caída o sin conexión?).")
    if not completo:
        print("   ⚠️  El listado quedó incompleto; se usa lo que alcanzó a llegar.")

    cache = cargar_cache()
    nuevas = [c for c in compras if c["codigo"] not in cache]
    print(f"2) {len(compras)} publicadas; {len(nuevas)} sin detalle → pidiendo su detalle...")

    # Compras que ya fallaron en varias corridas: se prueban 1 sola vez (no frenan la revisión).
    ruta_fallos = os.path.join(SALIDA, "fallos.json")
    try:
        with open(ruta_fallos, encoding="utf-8") as f:
            fallos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        fallos = {}
    dificiles = {c["codigo"] for c in nuevas if fallos.get(c["codigo"], 0) >= 3}

    def pedir_detalle(c):
        return c, llamar(f"/v2/compra-agil/{c['codigo']}", intentos=1 if c["codigo"] in dificiles else 3)

    def pasada(lista, hilos, etiqueta):
        fallidas = []
        with ThreadPoolExecutor(max_workers=hilos) as pool:
            futuros = [pool.submit(pedir_detalle, c) for c in lista]
            for i, fut in enumerate(as_completed(futuros), 1):
                c, det = fut.result()
                if det:
                    cache[c["codigo"]] = {
                        "nombre": det.get("nombre"),
                        "descripcion": det.get("descripcion"),
                        "productos_solicitados": [
                            {k: p.get(k) for k in ("codigo_producto", "nombre", "descripcion", "cantidad")}
                            for p in det.get("productos_solicitados") or []
                        ],
                        "visto": ahora.isoformat(timespec="seconds"),
                    }
                    estado = "✓"
                else:
                    fallidas.append(c)
                    estado = "✗"
                print(f"   {etiqueta}[{i}/{len(lista)}] {c['codigo']} {estado}")
                if i % 10 == 0:
                    guardar_cache(cache)
        guardar_cache(cache)
        return fallidas

    fallidas = pasada(nuevas, HILOS, "") if nuevas else []
    for ronda in range(1, REINTENTOS_FINALES + 1):
        fallidas = [c for c in fallidas if c["codigo"] not in dificiles]
        if not fallidas:
            break
        print(f"   🔁 Reintentando {len(fallidas)} que fallaron (ronda {ronda}/{REINTENTOS_FINALES}) en 60 s...")
        time.sleep(60)
        fallidas = pasada(fallidas, 2, f"reintento {ronda} ")
    if fallidas:
        print(f"   ⚠️  {len(fallidas)} compras siguen sin detalle; se intentan en la próxima corrida.")

    # Limpiar del caché las compras que ya no están abiertas (solo si el listado llegó completo).
    if completo:
        abiertas = {c["codigo"] for c in compras}
        for cod in [k for k in cache if k not in abiertas]:
            del cache[cod]
        guardar_cache(cache)

    filas = []
    for c in compras:
        det = cache.get(c["codigo"])
        sin_detalle = not det
        if sin_detalle:
            # La API no entrega el detalle: se evalúa solo por el título del listado.
            det = {"nombre": c.get("nombre"), "productos_solicitados": []}
        tipo, motivos = clasificar(det)
        if not tipo:
            continue
        inst = c.get("institucion") or {}
        fechas = c.get("fechas") or {}
        cierre = a_local(fechas.get("fecha_cierre"))
        pub = a_local(fechas.get("fecha_publicacion"))
        visto = a_local(det.get("visto")) if det.get("visto") else None
        if visto and visto.tzinfo is None:
            visto = visto.replace(tzinfo=ZONA)
        filas.append({
            "tipo": tipo,
            "motivos": motivos,
            "nueva": bool(visto and ahora - visto < timedelta(hours=HORAS_NUEVA)),
            "codigo": c["codigo"],
            "nombre": c.get("nombre") or "",
            "organismo": inst.get("organismo_comprador") or "",
            "unidad": inst.get("unidad_compra") or "",
            "monto": (c.get("montos") or {}).get("monto_disponible_clp"),
            "cierre_dt": cierre,
            "cierre": cierre.strftime("%d-%m %H:%M") if cierre else "",
            "cierre_iso": cierre.isoformat() if cierre else "",
            "publicacion": pub.strftime("%d-%m %H:%M") if pub else "",
            "publicacion_iso": pub.isoformat() if pub else "",
            "sin_detalle": sin_detalle,
            "ofertas": (c.get("resumen") or {}).get("total_ofertas_recibidas", 0),
            "productos": [] if sin_detalle else
                         [[str(p.get('codigo_producto') or ''), p.get('nombre') or '']
                          for p in det.get("productos_solicitados") or []],
            "link": f"https://buscador.mercadopublico.cl/ficha?code={c['codigo']}",
        })
    filas.sort(key=lambda f: f["cierre_iso"] or "9999")
    pendientes = [c for c in compras if c["codigo"] not in cache]

    os.makedirs(SALIDA, exist_ok=True)
    fallos = {c["codigo"]: fallos.get(c["codigo"], 0) + 1 for c in pendientes}
    with open(ruta_fallos, "w", encoding="utf-8") as f:
        json.dump(fallos, f)
    ruta_html = os.path.join(SALIDA, "index.html" if MODO_WEB else "radar_aysen.html")
    with open(ruta_html, "w", encoding="utf-8") as f:
        f.write(generar_html(filas, ahora, len(compras), pendientes))

    ruta_csv = os.path.join(SALIDA, "oportunidades.csv")
    with open(ruta_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["tipo", "nueva", "codigo", "nombre", "organismo", "monto_clp", "cierre", "motivos", "link"])
        for r in filas:
            w.writerow([r["tipo"], "sí" if r["nueva"] else "", r["codigo"], r["nombre"], r["organismo"],
                        r["monto"] or "", r["cierre"], ", ".join(r["motivos"]), r["link"]])

    ops = [r for r in filas if r["tipo"] == "OPORTUNIDAD"]
    pos = [r for r in filas if r["tipo"] == "POSIBLE"]
    print("\n==============================")
    print(f"✅ EN TUS RUBROS: {len(ops)}   🔎 POSIBLES: {len(pos)}")
    print("==============================")
    for r in ops:
        print(f"{'🆕 ' if r['nueva'] else ''}{r['codigo']} | cierra {r['cierre']} | {pesos(r['monto'])}")
        print(f"   {r['nombre']} — {r['organismo']}")
    print(f"\nReporte: {ruta_html}")
    if "--abrir" in sys.argv:
        webbrowser.open("file://" + ruta_html)


if __name__ == "__main__":
    main()
