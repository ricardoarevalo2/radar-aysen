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
CSS = """
:root{--bg:#f4f5f7;--card:#fff;--tx:#1d2330;--mu:#667085;--bd:#e4e7ec;--ac:#1a56db;
--ok:#067647;--okbg:#ecfdf3;--wa:#b54708;--wabg:#fffaeb;--rd:#b42318;--rdbg:#fef3f2;--chip:#f2f4f7}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--tx:#e6e8ec;--mu:#98a2b3;
--bd:#2a2f3a;--ac:#6ea8fe;--ok:#47cd89;--okbg:#0e2a1d;--wa:#fdb022;--wabg:#2b2111;--rd:#f97066;
--rdbg:#2d1614;--chip:#232833}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0}.sub{color:var(--mu);margin:4px 0 16px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px}
.kpi b{display:block;font-size:24px}.kpi span{color:var(--mu);font-size:12px}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.tools #q{flex:1;min-width:180px;padding:10px 12px;border-radius:10px;border:1px solid var(--bd);
background:var(--card);color:var(--tx);font-size:15px}
.tools label{display:flex;align-items:center;gap:6px;color:var(--mu);font-size:13px}
h2{font-size:16px;margin:24px 0 10px;display:flex;align-items:center;gap:8px}
h2 .n{background:var(--chip);border-radius:20px;padding:1px 9px;font-size:13px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px;
margin-bottom:10px;border-left:5px solid var(--bd)}
.card.urg{border-left-color:var(--rd)}.card.pro{border-left-color:var(--wa)}.card.ok{border-left-color:var(--ok)}
.card.hecha{opacity:.45}
.top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.tit{font-weight:600;margin:2px 0 4px}.org{color:var(--mu);font-size:13px}
.cod{font-size:12px;color:var(--ac);text-decoration:none;font-weight:600}
.monto{font-weight:700;white-space:nowrap;text-align:right}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{background:var(--chip);border-radius:20px;padding:2px 9px;font-size:12px;color:var(--mu)}
.chip.rubro{background:var(--okbg);color:var(--ok)}.chip.pal{background:var(--wabg);color:var(--wa)}
.cierre{font-size:13px;margin-top:8px}.cierre b.urg{color:var(--rd)}.cierre b.pro{color:var(--wa)}
.nueva{background:var(--rd);color:#fff;border-radius:6px;font-size:10px;padding:1px 6px;
font-weight:700;margin-left:6px;vertical-align:middle}
.prods{font-size:12px;color:var(--mu);margin-top:6px}
.acc{display:flex;gap:6px;margin-top:10px}
.acc a,.acc button{font-size:13px;border:1px solid var(--bd);background:transparent;color:var(--tx);
border-radius:8px;padding:6px 10px;cursor:pointer;text-decoration:none}
.acc a{background:var(--ac);color:#fff;border-color:var(--ac)}
.aviso{background:var(--wabg);color:var(--wa);border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:12px}
.aviso a{color:inherit}.vacio{color:var(--mu);font-size:14px}
footer{color:var(--mu);font-size:12px;margin-top:28px}
"""

JS = """
function cierreTxt(){const now=Date.now();document.querySelectorAll('[data-cierre]').forEach(el=>{
const t=Date.parse(el.dataset.cierre);if(isNaN(t))return;const h=(t-now)/36e5;let s,c='';
if(h<0){s='cerrada';c='urg'}else if(h<24){s='cierra en '+Math.max(1,Math.round(h))+' h';c='urg'}
else if(h<48){s='cierra mañana';c='pro'}else{s='cierra en '+Math.round(h/24)+' días'}
const b=el.querySelector('b.rel');b.textContent=s;b.className='rel '+c;
const card=el.closest('.card');if(card&&card.dataset.tipo==='OPORTUNIDAD'){card.classList.remove('urg','pro','ok');card.classList.add(c||'ok')}})}
function est(){try{return JSON.parse(localStorage.getItem('radar_estado')||'{}')}catch(e){return{}}}
function guardar(o){try{localStorage.setItem('radar_estado',JSON.stringify(o))}catch(e){}}
function marcar(cod,v){const o=est();if(o[cod]===v)delete o[cod];else o[cod]=v;guardar(o);pintar()}
function pintar(){const o=est(),ocultar=document.getElementById('ocultar').checked,q=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('.card').forEach(c=>{const cod=c.dataset.cod,v=o[cod];c.classList.toggle('hecha',!!v);
c.querySelectorAll('button[data-v]').forEach(b=>b.style.fontWeight=(b.dataset.v===v?'700':'400'));
const hit=!q||c.textContent.toLowerCase().includes(q);c.style.display=(hit&&!(ocultar&&v))?'':'none'})}
cierreTxt();pintar();setInterval(cierreTxt,60000);
"""


def tarjeta(f):
    nueva = "<span class='nueva'>NUEVA</span>" if f["nueva"] else ""
    clase_chip = "rubro" if f["tipo"] == "OPORTUNIDAD" else "pal"
    motivos = "".join(f"<span class='chip {clase_chip}'>{html.escape(m)}</span>" for m in f["motivos"])
    prods = " · ".join(html.escape(p) for p in f["productos"][:4])
    if len(f["productos"]) > 4:
        prods += f" · (+{len(f['productos']) - 4})"
    cod = html.escape(f["codigo"])
    return f"""<div class="card" data-cod="{cod}" data-tipo="{f['tipo']}">
<div class="top"><div><a class="cod" href="{f['link']}" target="_blank" rel="noopener">{cod}</a>{nueva}
<div class="tit">{html.escape(f['nombre'])}</div>
<div class="org">{html.escape(f['organismo'])} · {html.escape(f['unidad'])}</div></div>
<div class="monto">{pesos(f['monto'])}<div class="org">{f['ofertas']} ofertas</div></div></div>
<div class="cierre" data-cierre="{f['cierre_iso']}">⏱ <b class="rel"></b> · {f['cierre']} <span class="org">(publicada {f['publicacion']})</span></div>
<div class="prods">{prods}</div>
<div class="meta">{motivos}</div>
<div class="acc"><a href="{f['link']}" target="_blank" rel="noopener">Ver ficha</a>
<button data-v="cotizada" onclick="marcar('{cod}','cotizada')">✓ Cotizada</button>
<button data-v="descartada" onclick="marcar('{cod}','descartada')">✕ Descartar</button></div>
</div>"""


def generar_html(filas, ahora, total_region, pendientes=()):
    ops = [f for f in filas if f["tipo"] == "OPORTUNIDAD"]
    pos = [f for f in filas if f["tipo"] == "POSIBLE"]
    urgentes = [f for f in ops + pos if f["cierre_dt"] and f["cierre_dt"] - ahora < timedelta(hours=48)]
    monto_ops = sum(int(f["monto"] or 0) for f in ops)

    def seccion(titulo, lista, vacio):
        cuerpo = "".join(tarjeta(f) for f in lista) or f"<p class='vacio'>{vacio}</p>"
        return f"<h2>{titulo} <span class='n'>{len(lista)}</span></h2>{cuerpo}"

    aviso = ""
    if pendientes:
        items = "".join(
            f"<li><a href='https://buscador.mercadopublico.cl/ficha?code={html.escape(c['codigo'])}' "
            f"target='_blank' rel='noopener'>{html.escape(c['codigo'])}</a> · "
            f"{html.escape((c.get('nombre') or '')[:90])} · "
            f"<span class='org'>{html.escape((c.get('institucion') or {}).get('organismo_comprador') or '')}</span></li>"
            for c in pendientes)
        aviso = (f"<details class='aviso'><summary>ℹ️ {len(pendientes)} compras sin detalle disponible "
                 f"(Mercado Público no entrega sus productos; se revisaron solo por el título)</summary>"
                 f"<ul style='margin:8px 0 0;padding-left:18px'>{items}</ul></details>")

    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Radar Compra Ágil · Aysén</title><style>{CSS}</style></head><body><div class="wrap">
<h1>📡 Radar Compra Ágil · Aysén</h1>
<p class="sub">Actualizado {ahora:%d-%m-%Y %H:%M} · {total_region} compras abiertas en la región</p>
<div class="kpis">
<div class="kpi"><b>{len(ops)}</b><span>en tus rubros</span></div>
<div class="kpi"><b>{len(pos)}</b><span>posibles</span></div>
<div class="kpi"><b>{len(urgentes)}</b><span>cierran en &lt;48 h</span></div>
<div class="kpi"><b>{pesos(monto_ops)}</b><span>monto en tus rubros</span></div>
</div>
{aviso}
<div class="tools"><input id="q" placeholder="Buscar organismo, producto, código…" oninput="pintar()">
<label><input type="checkbox" id="ocultar" onchange="pintar()" checked> Ocultar cotizadas/descartadas</label></div>
{seccion("✅ En tus rubros", ops, "Nada abierto en tus rubros por ahora.")}
{seccion("🔎 Posibles (revisar a mano)", pos, "Nada por ahora.")}
<footer>Fuente: API Compra Ágil de Mercado Público · Región 11 · Rubros:
{html.escape(", ".join(RUBROS.values()))}. Las marcas “cotizada/descartada” se guardan solo en este navegador.</footer>
</div><script>{JS}</script></body></html>"""


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
            "ofertas": (c.get("resumen") or {}).get("total_ofertas_recibidas", 0),
            "productos": ["⚠ Detalle no disponible en la API: clasificada solo por el título"] if sin_detalle else
                         [f"{p.get('codigo_producto')} {p.get('nombre')}"
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
