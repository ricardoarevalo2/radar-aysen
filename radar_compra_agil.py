#!/usr/bin/env python3
"""
RADAR COMPRA ÁGIL — multi-empresa (v3)

Cada empresa es un archivo en la carpeta perfiles/ (ver perfiles/LEEME.md).
El radar consulta UNA vez las regiones que usan los perfiles y genera una página por empresa:
  - perfil con "carpeta": ""        → docs/index.html          (página principal)
  - perfil con "carpeta": "z-xxxx"  → docs/z-xxxx/index.html   (página de esa empresa)

Uso en el Mac:   python3 radar_compra_agil.py          (genera reportes/<carpeta>/...)
En GitHub:       lo corre .github/workflows/radar.yml  (genera docs/...)

El detalle de cada compra se guarda en cache_compras.json (compartido entre empresas):
cada corrida solo consulta las compras nuevas o las que fallaron la vez anterior.
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
ZONA = timezone(timedelta(hours=-3))  # hora de Chile continental (verano/invierno aprox.)
CARPETA_PERFILES = os.path.join(CARPETA, "perfiles")
try:
    from zoneinfo import ZoneInfo
    ZONA_MP = ZoneInfo("America/Santiago")   # zona horaria en que Mercado Público publica sus fechas
except Exception:
    ZONA_MP = ZONA
REGIONES = {
    1: "Región de Tarapacá", 2: "Región de Antofagasta", 3: "Región de Atacama", 4: "Región de Coquimbo",
    5: "Región de Valparaíso", 6: "Región de O'Higgins", 7: "Región del Maule", 8: "Región del Biobío",
    9: "Región de La Araucanía", 10: "Región de Los Lagos", 11: "Región de Aysén", 12: "Región de Magallanes",
    13: "Región Metropolitana", 14: "Región de Los Ríos", 15: "Región de Arica y Parinacota", 16: "Región de Ñuble",
}
BASE_URL = "https://api2.mercadopublico.cl"
CACHE = os.path.join(CARPETA, "cache_compras.json")
SALIDA = os.path.join(CARPETA, "docs" if MODO_WEB else "reportes")
PAUSA = 0.4             # segundos entre páginas del listado
HILOS = 4               # detalles consultados en paralelo (la API tarda ~20 s por detalle)
REINTENTOS_FINALES = 2  # rondas extra para las compras que fallaron
HORAS_NUEVA = 24        # una compra se marca NUEVA durante sus primeras 24 h en el radar
DIAS_HISTORIAL = 7     # días que se conserva el historial de compras cerradas
HORAS_REVISION_ADJ = 6  # horas mínimas entre revisiones de adjudicación por compra
MAX_ADJ_POR_CORRIDA = 5 # máximo de consultas de adjudicación por corrida
HORAS_GRACIA = 48       # horas que se mantiene visible una compra que desaparece del API (ej: 2º llamado)
# ─────────────────────────────────────────

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


def listar_publicadas(region):
    """Devuelve (compras, completo). completo=False si alguna página no respondió."""
    compras, pagina = [], 1
    while True:
        payload = llamar("/v2/compra-agil", {
            "region": region,
            "estado": "publicada",
            "tamano_pagina": 10,
            "numero_pagina": pagina,
        })
        if not payload:
            return compras, False
        items = payload.get("items", [])
        compras.extend(items)
        total_pag = (payload.get("paginacion") or {}).get("total_paginas", 1)
        print(f"   Región {region} · página {pagina}/{total_pag} — {len(compras)} compras")
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


def cargar_perfiles():
    perfiles = []
    for nombre in sorted(os.listdir(CARPETA_PERFILES)):
        if not nombre.endswith(".json"):
            continue
        with open(os.path.join(CARPETA_PERFILES, nombre), encoding="utf-8") as f:
            p = json.load(f)
        if p.get("activo", True) is False:
            continue
        p["id"] = nombre[:-5]
        p["regiones"] = [int(r) for r in p.get("regiones", [])]
        p["rubros"] = {str(k): v for k, v in p.get("rubros", {}).items()}
        p["prefijos"] = {c[:6]: n for c, n in p["rubros"].items()}
        p["palabras"] = [w.lower() for w in p.get("palabras", [])]
        p["excluir"] = [w.lower() for w in p.get("excluir", [])]
        p["patrones"] = {w: re.compile(r"\b" + re.escape(w) + r"\b") for w in p["palabras"]}
        p["carpeta"] = p.get("carpeta", "").strip("/")
        perfiles.append(p)
    if not perfiles:
        sys.exit("❌ No hay perfiles en la carpeta perfiles/.")
    return perfiles


def clasificar(detalle, perfil):
    """Devuelve ('OPORTUNIDAD'|'POSIBLE'|None, motivos) para una empresa."""
    rubros_hit = set()
    for p in detalle.get("productos_solicitados") or []:
        cod = str(p.get("codigo_producto") or "").strip()
        if cod[:6] in perfil["prefijos"]:
            rubros_hit.add(perfil["prefijos"][cod[:6]])
    if rubros_hit:
        return "OPORTUNIDAD", sorted(rubros_hit)

    texto = " ".join([
        detalle.get("nombre") or "",
        detalle.get("descripcion") or "",
        " ".join((p.get("nombre") or "") + " " + (p.get("descripcion") or "")
                 for p in detalle.get("productos_solicitados") or []),
    ]).lower()
    if any(x in texto for x in perfil["excluir"]):
        return None, []
    hits = sorted(w for w, rx in perfil["patrones"].items() if rx.search(texto))
    if hits:
        return "POSIBLE", hits
    return None, []


def a_local(iso):
    if not iso:
        return None
    try:
        # La API entrega la hora de Chile continental aunque venga marcada como UTC ("Z")
        # o sin zona: se toma la hora tal cual y se le asigna la zona de Santiago.
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)
        return d.replace(tzinfo=ZONA_MP)
    except ValueError:
        return None


def pesos(n):
    try:
        return "$" + f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _iso_a_dt(iso_str):
    """Convierte un string ISO a datetime con zona, o None si falla."""
    if not iso_str:
        return None
    try:
        d = datetime.fromisoformat(iso_str)
        if d.tzinfo is None:
            d = d.replace(tzinfo=ZONA)
        return d
    except (ValueError, TypeError):
        return None


# ───────────── HISTORIAL DE COMPRAS CERRADAS ─────────────

def ruta_historial(perfil):
    destino = os.path.join(SALIDA, perfil["carpeta"]) if perfil["carpeta"] else SALIDA
    return os.path.join(destino, "historial.json")


def cargar_historial(perfil):
    try:
        with open(ruta_historial(perfil), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar_historial(perfil, historial):
    destino = os.path.join(SALIDA, perfil["carpeta"]) if perfil["carpeta"] else SALIDA
    os.makedirs(destino, exist_ok=True)
    with open(ruta_historial(perfil), "w", encoding="utf-8") as f:
        json.dump(historial, f, ensure_ascii=False, indent=1)


def actualizar_historial(historial, filas, codigos_region, ahora):
    """
    Actualiza el historial de compras:
    - Elimina entradas más antiguas que DIAS_HISTORIAL.
    - Actualiza metadatos de compras que siguen activas en el radar.
    - Marca como 'removida' las que ya no aparecen en la región.
    - Agrega nuevas compras del radar que no estaban en el historial.
    """
    limite = ahora - timedelta(days=DIAS_HISTORIAL)
    codigos_activos = {f["codigo"] for f in filas}
    filas_por_codigo = {f["codigo"]: f for f in filas}

    for cod in list(historial.keys()):
        h = historial[cod]
        ref = _iso_a_dt(h.get("cierre_iso") or h.get("primera_vista_iso"))
        if ref and ref < limite:
            del historial[cod]
            continue
        if cod in codigos_activos:
            # Sigue activa en el radar: actualizar metadatos
            fila = filas_por_codigo[cod]
            h.update({
                "nombre": fila["nombre"],
                "organismo": fila["organismo"],
                "unidad": fila.get("unidad", ""),
                "monto": fila["monto"],
                "cierre_iso": fila["cierre_iso"],
                "tipo": fila["tipo"],
                "motivos": fila["motivos"],
                "ultima_activa_iso": ahora.isoformat(timespec="seconds"),
                "removida_iso": None,
            })
        elif cod in codigos_region:
            # Está en la región pero no en el filtro: sigue "viva"
            h["ultima_activa_iso"] = ahora.isoformat(timespec="seconds")
        elif not h.get("removida_iso"):
            # Ya no está en la región: marcar como removida
            h["removida_iso"] = ahora.isoformat(timespec="seconds")

    # Agregar compras nuevas del radar que no estaban en el historial
    for fila in filas:
        cod = fila["codigo"]
        if cod not in historial:
            historial[cod] = {
                "codigo": cod,
                "nombre": fila["nombre"],
                "organismo": fila["organismo"],
                "unidad": fila.get("unidad", ""),
                "monto": fila["monto"],
                "cierre_iso": fila["cierre_iso"],
                "tipo": fila["tipo"],
                "motivos": fila["motivos"],
                "link": fila["link"],
                "primera_vista_iso": ahora.isoformat(timespec="seconds"),
                "ultima_activa_iso": ahora.isoformat(timespec="seconds"),
                "removida_iso": None,
                "estado_api": None,
                "adj_nombre": None,
                "adj_monto": None,
                "adj_iso": None,
                "adj_revisado_iso": None,
            }
    return historial


def verificar_adjudicaciones(historial, ahora):
    """
    Consulta la API de detalle para compras cerradas que aún no tienen
    estado de adjudicación confirmado, con límite de MAX_ADJ_POR_CORRIDA.
    """
    umbral = ahora - timedelta(hours=HORAS_REVISION_ADJ)
    estados_finales = {"adjudicada", "cerrada", "desierta", "cancelada", "revocada"}
    pendientes = []

    for cod, h in historial.items():
        if not h.get("removida_iso"):
            continue  # solo compras que ya salieron del listado
        # Si ya tiene estado final con nombre de proveedor, no revisitar
        if (h.get("estado_api") or "").lower() in estados_finales and h.get("adj_nombre"):
            continue
        # Respetar tiempo mínimo entre revisiones
        ultima = _iso_a_dt(h.get("adj_revisado_iso"))
        if ultima and ultima >= umbral:
            continue
        pendientes.append(cod)

    if not pendientes:
        return historial

    lote = pendientes[:MAX_ADJ_POR_CORRIDA]
    print(f"   🔍 Verificando adjudicación de {len(lote)} compra(s) cerrada(s)...")

    for cod in lote:
        det = llamar(f"/v2/compra-agil/{cod}", intentos=1)
        historial[cod]["adj_revisado_iso"] = ahora.isoformat(timespec="seconds")
        if det:
            estado_raw = det.get("estado") or det.get("status") or ""
            estado = (estado_raw if isinstance(estado_raw, str) else "").lower()
            historial[cod]["estado_api"] = estado
            # Intentar extraer adjudicación con varios nombres de campo posibles
            adjs = det.get("adjudicaciones") or det.get("adjudicacion") or []
            if isinstance(adjs, dict):
                adjs = [adjs]
            elif not isinstance(adjs, list):
                adjs = []
            if adjs:
                adj = adjs[0]
                historial[cod]["adj_nombre"] = (
                    adj.get("nombre_proveedor") or adj.get("nombre") or
                    adj.get("razon_social") or adj.get("nombre_empresa") or
                    adj.get("proveedor")
                )
                historial[cod]["adj_monto"] = (
                    adj.get("monto") or adj.get("monto_adjudicado") or adj.get("monto_clp")
                )
                historial[cod]["adj_iso"] = (
                    adj.get("fecha_adjudicacion") or adj.get("fecha") or adj.get("fecha_resolucion")
                )
        time.sleep(0.5)

    return historial


# ───────────── REPORTE HTML ─────────────
# El diseño vive en plantilla.html (junto a este script). Aquí solo se preparan los datos
# y se reemplaza __DATOS__ por el JSON. Para cambiar el look, edita plantilla.html.
PLANTILLA = os.path.join(CARPETA, "plantilla.html")


def preparar_datos(perfil, filas, ahora, total_region, hist_cerradas=(), pendientes=()):
    return {
        "actualizado": ahora.isoformat(),
        "empresa": perfil.get("nombre", ""),
        "region": " · ".join(REGIONES.get(r, f"Región {r}") for r in perfil["regiones"]),
        "total_region": total_region,
        "rubros": perfil["rubros"],
        "filas": [{
            **{k: f[k] for k in ("tipo", "motivos", "nueva", "codigo", "nombre", "organismo", "unidad", "monto",
                                  "cierre_iso", "publicacion", "publicacion_iso", "ofertas", "productos", "link",
                                  "sin_detalle", "descripcion")},
            "en_gracia": f.get("en_gracia", False),
            "desaparecida_iso": f.get("desaparecida_iso", ""),
        } for f in filas],
        "historial": [
            {k: h.get(k) for k in ("codigo", "nombre", "organismo", "monto", "cierre_iso", "tipo",
                                   "motivos", "link", "primera_vista_iso", "removida_iso",
                                   "estado_api", "adj_nombre", "adj_monto", "adj_iso")}
            for h in hist_cerradas
        ],
        "pendientes": [{"codigo": c["codigo"], "nombre": (c.get("nombre") or "")[:110],
                        "organismo": (c.get("institucion") or {}).get("organismo_comprador") or "",
                        "link": f"https://buscador.mercadopublico.cl/ficha?code={c['codigo']}"} for c in pendientes],
    }


def generar_html(datos):
    try:
        with open(PLANTILLA, encoding="utf-8") as f:
            plantilla = f.read()
    except FileNotFoundError:
        sys.exit("❌ No encuentro plantilla.html junto al script.")
    marcador = 'id="datos">__DATOS__</script>'
    if marcador not in plantilla:
        sys.exit("❌ plantilla.html no tiene el marcador __DATOS__ (debe estar dentro de <script id=\"datos\">).")
    js = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    antes, _, despues = plantilla.rpartition(marcador)  # la última aparición es la real
    return antes + 'id="datos">' + js + '</script>' + despues


def main():
    ahora = datetime.now(ZONA)
    perfiles = cargar_perfiles()
    regiones = sorted({r for p in perfiles for r in p["regiones"]})
    print(f"📡 RADAR COMPRA ÁGIL — {len(perfiles)} empresa(s), regiones {regiones}")
    print("1) Buscando compras publicadas...")
    compras, completo = [], True
    for region in regiones:
        lote, ok = listar_publicadas(region)
        for c in lote:
            c["_region"] = region
        compras.extend(lote)
        completo = completo and ok
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
                        "_region": c.get("_region"),
                        "_cierre_iso": ((c.get("fechas") or {}).get("fecha_cierre") or ""),
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
    # Las compras que desaparecen se marcan con _desaparecida_iso y se conservan HORAS_GRACIA horas
    # antes de borrarlas, para cubrir el caso de 2º llamado o reactivaciones.
    if completo:
        abiertas = {c["codigo"] for c in compras}
        # Actualizar _cierre_iso en cache con el dato fresco del listado
        cierre_lista = {c["codigo"]: ((c.get("fechas") or {}).get("fecha_cierre") or "") for c in compras}
        for cod, det in cache.items():
            if cod in cierre_lista and cierre_lista[cod]:
                det["_cierre_iso"] = cierre_lista[cod]
        for cod in list(cache.keys()):
            if cod in abiertas:
                # Está activa en el listado: limpiar cualquier marca previa
                cache[cod].pop("_desaparecida_iso", None)
                cache[cod].pop("_cierre_2do_iso", None)
            elif not cache[cod].get("_desaparecida_iso"):
                # Acaba de desaparecer: consultar la API para ver si hay un 2º llamado
                det_actual = llamar(f"/v2/compra-agil/{cod}", intentos=1)
                cierre_nuevo = None
                if det_actual:
                    cierre_nuevo = (det_actual.get("fechas") or {}).get("fecha_cierre")
                if cierre_nuevo:
                    cierre_dt = a_local(cierre_nuevo)
                    if cierre_dt and cierre_dt > ahora:
                        # 2º llamado activo: guardar nueva fecha y marcar
                        cache[cod]["_desaparecida_iso"] = ahora.isoformat(timespec="seconds")
                        cache[cod]["_cierre_2do_iso"] = cierre_dt.isoformat()
                        print(f"   🔄 {cod}: no está en lista pero cierra {cierre_dt:%d-%m %H:%M} → zona de gracia")
                    else:
                        del cache[cod]  # cerrada de verdad
                else:
                    del cache[cod]  # API no la devuelve o sin fecha
            else:
                # Ya estaba en zona de gracia: revisar si el 2º llamado sigue vigente
                cierre_2do = cache[cod].get("_cierre_2do_iso")
                if cierre_2do:
                    try:
                        cierre_dt = datetime.fromisoformat(cierre_2do)
                        if cierre_dt.tzinfo is None:
                            cierre_dt = cierre_dt.replace(tzinfo=ZONA)
                        if ahora >= cierre_dt:
                            del cache[cod]  # 2º llamado cerrado
                    except (ValueError, KeyError):
                        del cache[cod]
                else:
                    del cache[cod]
        guardar_cache(cache)

    pendientes_todas = [c for c in compras if c["codigo"] not in cache]
    os.makedirs(SALIDA, exist_ok=True)
    fallos = {c["codigo"]: fallos.get(c["codigo"], 0) + 1 for c in pendientes_todas}
    with open(ruta_fallos, "w", encoding="utf-8") as f:
        json.dump(fallos, f)

    for perfil in perfiles:
        generar_perfil(perfil, [c for c in compras if c["_region"] in perfil["regiones"]], cache, ahora)


def generar_perfil(perfil, compras, cache, ahora):
    filas = []
    for c in compras:
        det = cache.get(c["codigo"])
        sin_detalle = not det
        if sin_detalle:
            # La API no entrega el detalle: se evalúa solo por el título del listado.
            det = {"nombre": c.get("nombre"), "productos_solicitados": []}
        tipo, motivos = clasificar(det, perfil)
        if not tipo:
            continue
        inst = c.get("institucion") or {}
        fechas = c.get("fechas") or {}
        cierre = a_local(fechas.get("fecha_cierre"))
        pub = a_local(fechas.get("fecha_publicacion"))
        try:
            visto = datetime.fromisoformat(det["visto"]) if det.get("visto") else None
        except ValueError:
            visto = None
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
                         [[str(p.get('codigo_producto') or ''), p.get('nombre') or '',
                           p.get('cantidad'), (p.get('descripcion') or '').strip()]
                          for p in det.get("productos_solicitados") or []],
            "descripcion": "" if sin_detalle else (det.get("descripcion") or "").strip(),
            "link": f"https://buscador.mercadopublico.cl/ficha?code={c['codigo']}",
        })
    # Agregar compras en zona de gracia: desaparecieron del API pero aún no vencen las HORAS_GRACIA.
    codigos_activos = {c["codigo"] for c in compras}
    for cod, det in cache.items():
        if cod in codigos_activos:
            continue
        desap_iso = det.get("_desaparecida_iso")
        if not desap_iso:
            continue
        region_cache = det.get("_region")
        if region_cache not in perfil["regiones"]:
            continue
        tipo, motivos = clasificar(det, perfil)
        if not tipo:
            continue
        filas.append({
            "tipo": tipo,
            "motivos": motivos,
            "nueva": False,
            "codigo": cod,
            "nombre": det.get("nombre") or "",
            "organismo": "",
            "unidad": "",
            "monto": None,
            "cierre_dt": None,
            "cierre": "",
            "cierre_iso": "",
            "publicacion": "",
            "publicacion_iso": "",
            "sin_detalle": False,
            "ofertas": 0,
            "productos": [[str(p.get("codigo_producto") or ""), p.get("nombre") or "",
                           p.get("cantidad"), (p.get("descripcion") or "").strip()]
                          for p in det.get("productos_solicitados") or []],
            "descripcion": (det.get("descripcion") or "").strip(),
            "link": f"https://buscador.mercadopublico.cl/ficha?code={cod}",
            "en_gracia": True,   # flag para la plantilla
            "desaparecida_iso": desap_iso,
        })

    filas.sort(key=lambda f: f["cierre_iso"] or "9999")
    pendientes = [c for c in compras if c["codigo"] not in cache]

    # ── Historial de compras cerradas ──
    codigos_region = {c["codigo"] for c in compras}
    historial = cargar_historial(perfil)
    historial = actualizar_historial(historial, filas, codigos_region, ahora)
    historial = verificar_adjudicaciones(historial, ahora)
    guardar_historial(perfil, historial)
    hist_cerradas = sorted(
        [h for h in historial.values() if h.get("removida_iso")],
        key=lambda h: h.get("cierre_iso") or "",
        reverse=True,
    )

    destino = os.path.join(SALIDA, perfil["carpeta"]) if perfil["carpeta"] else SALIDA
    os.makedirs(destino, exist_ok=True)
    ruta_html = os.path.join(destino, "index.html")
    datos = preparar_datos(perfil, filas, ahora, len(compras), hist_cerradas, pendientes)
    with open(ruta_html, "w", encoding="utf-8") as f:
        f.write(generar_html(datos))
    with open(os.path.join(destino, "datos.json"), "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    with open(os.path.join(destino, "oportunidades.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["tipo", "nueva", "codigo", "nombre", "organismo", "monto_clp", "cierre", "motivos", "link"])
        for r in filas:
            w.writerow([r["tipo"], "sí" if r["nueva"] else "", r["codigo"], r["nombre"], r["organismo"],
                        r["monto"] or "", r["cierre"], ", ".join(r["motivos"]), r["link"]])

    ops = [r for r in filas if r["tipo"] == "OPORTUNIDAD"]
    pos = [r for r in filas if r["tipo"] == "POSIBLE"]
    print(f"\n=== {perfil.get('nombre', perfil['id'])}: ✅ {len(ops)} en rubros · 🔎 {len(pos)} posibles → {ruta_html}")
    if hist_cerradas:
        print(f"   📋 Historial: {len(hist_cerradas)} compra(s) cerrada(s) en seguimiento")
    for r in ops:
        print(f"   {'🆕 ' if r['nueva'] else ''}{r['codigo']} | cierra {r['cierre']} | {pesos(r['monto'])} | {r['nombre'][:60]}")
    if "--abrir" in sys.argv:
        webbrowser.open("file://" + ruta_html)


if __name__ == "__main__":
    main()
