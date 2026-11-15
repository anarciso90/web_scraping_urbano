"""
Scraper de Urbania.pe para TODA Lima Metropolitana (v04)
=========================================================

Cambios respecto a v03:
  - Varias operaciones (alquiler y venta) y tipos de inmueble en una sola corrida;
    columna `operacion` y precio tomado de la operación correcta del aviso.
  - Una búsqueda POR DISTRITO (no un slug gigante con 43 distritos unidos por "-o-").
  - Checkpoint por distrito: si se corta, al relanzar continúa donde quedó.
  - Reintentos con backoff ante 429 / 5xx / errores de red (antes: un error = fin).
  - Parseo del JSON con raw_decode (más robusto que el regex no-greedy).
  - Validación del slug: avisa si los anuncios devueltos no pertenecen al distrito
    pedido (slug mal escrito -> el portal puede devolver otra búsqueda sin error).
  - Aviso si el portal recorta la paginación (total > lo que se puede paginar).
  - Todas las características (mainFeatures) se aplanan en columnas, no solo CFT100.
  - Precio por m² en la moneda en que viene (alquileres suelen venir en soles).
  - Columna `coord_repetida` para detectar ubicaciones aproximadas/centroides.
  - Exporta CSV (sep='|') y, si geopandas está instalado, GeoPackage en EPSG:4326.

REQUISITOS mínimos:  pip install curl_cffi pandas      (opcional: geopandas)
USO:                 python scraper_urbania_v04.py
"""

import json
import random
import re
import time
import unicodedata
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

OPERACIONES = ["alquiler", "venta"]   # se scrapean todas las combinaciones
TIPOS = ["departamentos"]             # se puede ampliar: "casas", "terrenos", ...

# 43 distritos de la provincia de Lima. OJO: los slugs siguen la convención de
# Urbania (minúsculas, sin tildes, guiones) pero NO están todos verificados.
# Corre primero con VALIDAR_SOLO_PAGINA_1 = True y revisa el reporte de validación.
DISTRITOS_LIMA = [
    "ancon", "ate", "barranco", "brena", "carabayllo", "chaclacayo",
    "chorrillos", "cieneguilla", "comas", "el-agustino", "independencia",
    "jesus-maria", "la-molina", "la-victoria", "cercado-de-lima", "lince",
    "los-olivos", "lurigancho", "lurin", "magdalena-del-mar", "miraflores",
    "pachacamac", "pucusana", "pueblo-libre", "puente-piedra", "punta-hermosa",
    "punta-negra", "rimac", "san-bartolo", "san-borja", "san-isidro",
    "san-juan-de-lurigancho", "san-juan-de-miraflores", "san-luis",
    "san-martin-de-porres", "san-miguel", "santa-anita", "santa-maria-del-mar",
    "santa-rosa", "santiago-de-surco", "surquillo", "villa-el-salvador",
    "villa-maria-del-triunfo",
]
# Si tu ámbito es Lima-Callao, añade los 7 distritos del Callao. El sufijo de
# provincia/departamento probablemente cambia ("--callao--callao"): verificar.
DISTRITOS_CALLAO = [
    "callao", "bellavista", "carmen-de-la-legua-reynoso", "la-perla",
    "la-punta", "ventanilla", "mi-peru",
]
INCLUIR_CALLAO = False

SUFIJO = {"lima": "--lima--lima", "callao": "--callao--callao"}

VALIDAR_SOLO_PAGINA_1 = False   # True = solo pide la página 1 de cada distrito
MAX_PAGINAS = 200               # tope de seguridad por distrito
POR_PAGINA = 30                 # resultados por página que devuelve Urbania
PAUSA_MIN_SEG, PAUSA_MAX_SEG = 2.5, 5.0
MAX_REINTENTOS = 4

# Resultados en una carpeta JUNTO AL SCRIPT (no en la carpeta desde donde se ejecuta)
OUT_DIR = Path(__file__).resolve().parent / "urbania_lima_metropolitana"
CHECKPOINT_DIR = OUT_DIR / "por_distrito"
OUTPUT_CSV = OUT_DIR / "urbania_lima_metropolitana.csv"
OUTPUT_GPKG = OUT_DIR / "urbania_lima_metropolitana.gpkg"   # una capa por operación_tipo

HEADERS = {
    # El User-Agent lo pone curl_cffi según `impersonate`; si lo fijas a mano
    # puede quedar desalineado con la huella TLS.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
    "Referer": "https://urbania.pe/",
}


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------

def slugify(texto: str) -> str:
    """'Magdalena del Mar' -> 'magdalena-del-mar' (para comparar con el slug)."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def construir_url(operacion: str, tipo: str, slug: str, provincia: str, pagina: int) -> str:
    base = f"https://urbania.pe/buscar/{operacion}-de-{tipo}-en-{slug}{SUFIJO[provincia]}"
    return base if pagina == 1 else f"{base}?page={pagina}"


def get_con_reintentos(session, url: str):
    """GET con backoff exponencial ante 429/5xx/errores de red."""
    for intento in range(1, MAX_REINTENTOS + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
        except Exception as e:  # noqa: BLE001
            print(f"    Error de red (intento {intento}): {e}")
            resp = None
        if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        espera = (2 ** intento) * 5 + random.uniform(0, 3)
        print(f"    Reintentando en {espera:.0f}s...")
        time.sleep(espera)
    return resp


# ---------------------------------------------------------------------------
# EXTRACCIÓN
# ---------------------------------------------------------------------------

def extraer_preloaded_state(html: str):
    """
    Ubica `window.__PRELOADED_STATE__ =` y decodifica exactamente un objeto
    JSON a partir de ahí con raw_decode (no depende de cómo termine la línea).
    """
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        return obj
    except json.JSONDecodeError as e:
        print(f"    [!] Error parseando __PRELOADED_STATE__: {e}")
        return None


def extraer_listados(state: dict):
    list_store = state.get("listStore", {}) or {}
    return list_store.get("listPostings", []) or [], list_store.get("paging", {}) or {}


def cadena_ubicacion(location_info: dict):
    """Recorre location -> parent -> parent... y devuelve [(label, name), ...]."""
    cadena, nodo, guard = [], location_info, 0
    while nodo and guard < 10:
        cadena.append((nodo.get("label"), nodo.get("name")))
        nodo, guard = nodo.get("parent"), guard + 1
    return cadena


def elegir_operacion(ops: list, operacion: str):
    """
    Un aviso puede publicarse en venta Y alquiler a la vez: priceOperationTypes
    trae una entrada por operación. v03 tomaba siempre ops[0], así que en una
    búsqueda de venta podía guardar el precio de alquiler (o al revés).
    Aquí se busca la entrada cuyo operationType coincide con la búsqueda.
    """
    for op in ops:
        nombre = slugify((op.get("operationType") or {}).get("name", ""))
        if nombre == operacion:
            return op, True
    return (ops[0] if ops else {}), False


def normalizar_posting(p: dict, distrito_busqueda: str, operacion: str, tipo: str) -> dict:
    # --- Precio de la operación buscada (normalmente en UNA sola moneda) ---
    precio_soles = precio_dolares = None
    ops = p.get("priceOperationTypes") or []
    op, op_confirmada = elegir_operacion(ops, operacion)
    if op:
        for precio in op.get("prices", []) or []:
            if precio.get("currency") == "S/":
                precio_soles = precio.get("amount")
            elif precio.get("currency") == "USD":
                precio_dolares = precio.get("amount")

    # --- Características: TODAS las de mainFeatures, como columnas ---
    feats = {}
    for codigo, f in (p.get("mainFeatures") or {}).items():
        if not isinstance(f, dict):
            continue
        nombre = slugify(f.get("label") or codigo).replace("-", "_")
        feats[f"feat_{nombre}"] = f.get("value")
        feats[f"feat_{nombre}_cod"] = codigo  # para saber qué CFT es cada una

    def _float(cod):
        try:
            return float(((p.get("mainFeatures") or {}).get(cod) or {}).get("value"))
        except (TypeError, ValueError):
            return None

    area_total = _float("CFT100")     # superficie/área total
    area_techada = _float("CFT101")   # área techada/cubierta (verificar en tu data)
    area_ref = area_techada or area_total

    # --- Ubicación ---
    pl = p.get("postingLocation") or {}
    direccion = (pl.get("address") or {}).get("name")
    cadena = cadena_ubicacion(pl.get("location") or {})
    subzona = next((n for lab, n in cadena if lab == "SUBZONA"), None)
    distrito = next((n for lab, n in cadena if lab != "SUBZONA"), None)

    geo = (pl.get("postingGeolocation") or {}).get("geolocation") or {}

    tit = p.get("generatedTitle") or ""
    fila = {
        "posting_id": p.get("postingId"),
        "operacion": operacion,
        "tipo_busqueda": tipo,
        "operacion_confirmada": op_confirmada,   # False = no se halló la operación en el aviso
        "multi_operacion": len(ops) > 1,         # True = el aviso está en venta y alquiler
        "distrito_busqueda": distrito_busqueda,
        "titulo": p.get("title"),
        "tipo_inmueble": tit.split(" · ")[0].strip() if tit else None,
        "tipo_inmueble_categoria": (p.get("realEstateType") or {}).get("name"),
        "precio_soles": precio_soles,
        "precio_dolares": precio_dolares,
        "area_total_m2": area_total,
        "area_techada_m2": area_techada,
        "soles_por_m2": round(precio_soles / area_ref, 2) if precio_soles and area_ref else None,
        "dolares_por_m2": round(precio_dolares / area_ref, 2) if precio_dolares and area_ref else None,
        "distrito": distrito,
        "subzona": subzona,
        "ubicacion_jerarquia": " > ".join(f"{lab}:{n}" for lab, n in cadena),
        "direccion": direccion,
        "lat": geo.get("latitude"),
        "lng": geo.get("longitude"),
        "publicador": (p.get("publisher") or {}).get("name"),
        "link": f"https://urbania.pe{p.get('url')}" if p.get("url") else None,
        "fecha_modificacion": p.get("modified_date"),
        "descripcion_completa": p.get("descriptionNormalized"),
    }
    fila.update(feats)
    return fila


# ---------------------------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------------------------

def scrapear_distrito(session, operacion: str, tipo: str, slug: str, provincia: str):
    filas = []
    reporte = {"operacion": operacion, "tipo": tipo, "distrito_busqueda": slug,
               "total_portal": None, "paginas_portal": None, "obtenidos": 0, "estado": "ok"}
    tope = 1 if VALIDAR_SOLO_PAGINA_1 else MAX_PAGINAS

    for pagina in range(1, tope + 1):
        url = construir_url(operacion, tipo, slug, provincia, pagina)
        print(f"[+] {operacion}/{tipo} | {slug} | página {pagina}: {url}")
        resp = get_con_reintentos(session, url)

        if resp is None or resp.status_code != 200:
            code = getattr(resp, "status_code", "sin respuesta")
            print(f"    Status {code}. Deteniendo este distrito.")
            if resp is not None:
                (OUT_DIR / f"debug_{operacion}_{tipo}_{slug}_p{pagina}.html").write_text(
                    resp.text, encoding="utf-8")
            reporte["estado"] = f"error_{code}"
            break

        # Redirección silenciosa = slug probablemente inválido
        if pagina == 1 and slug not in str(resp.url):
            print(f"    [!] Redirigido a {resp.url} -- revisa el slug '{slug}'")
            reporte["estado"] = "redirigido"

        state = extraer_preloaded_state(resp.text)
        if not state:
            (OUT_DIR / f"debug_{operacion}_{tipo}_{slug}_p{pagina}.html").write_text(
                resp.text, encoding="utf-8")
            reporte["estado"] = "sin_state"
            break

        postings, paging = extraer_listados(state)
        if pagina == 1:
            reporte["total_portal"] = paging.get("total")
            reporte["paginas_portal"] = paging.get("totalPages")
        if not postings:
            break

        filas.extend(normalizar_posting(p, slug, operacion, tipo) for p in postings)
        print(f"    {len(postings)} anuncios (pág {paging.get('currentPage')}/"
              f"{paging.get('totalPages')}, total {paging.get('total')})")

        if paging.get("lastPage"):
            break
        time.sleep(random.uniform(PAUSA_MIN_SEG, PAUSA_MAX_SEG))

    reporte["obtenidos"] = len({f["posting_id"] for f in filas})

    # Validación: ¿los anuncios son del distrito pedido?
    if filas:
        coincide = sum(slugify(f["distrito"]) == slug for f in filas) / len(filas)
        reporte["pct_distrito_coincide"] = round(100 * coincide, 1)
        if coincide < 0.5:
            print(f"    [!] Solo {coincide:.0%} de anuncios dicen ser de '{slug}'. "
                  "Revisa el slug o cómo nombra Urbania ese distrito.")

    # ¿El portal recortó la paginación?
    total = reporte["total_portal"] or 0
    if not VALIDAR_SOLO_PAGINA_1 and total and reporte["obtenidos"] < 0.9 * total:
        print(f"    [!] Portal reporta {total} pero se obtuvieron {reporte['obtenidos']}. "
              "Posible tope de paginación: subdivide (por rango de precio, dormitorios, etc.).")
    return filas, reporte


def scrapear():
    OUT_DIR.mkdir(exist_ok=True)
    CHECKPOINT_DIR.mkdir(exist_ok=True)
    session = cffi_requests.Session(impersonate="chrome")

    distritos = [(s, "lima") for s in DISTRITOS_LIMA]
    if INCLUIR_CALLAO:
        distritos += [(s, "callao") for s in DISTRITOS_CALLAO]
    objetivos = [(op, tp, s, prov) for op in OPERACIONES for tp in TIPOS
                 for s, prov in distritos]
    print(f"{len(objetivos)} búsquedas: {OPERACIONES} x {TIPOS} x {len(distritos)} distritos")

    reportes = []
    for operacion, tipo, slug, provincia in objetivos:
        ckpt = CHECKPOINT_DIR / f"{operacion}_{tipo}_{slug}.csv"
        if ckpt.exists() and not VALIDAR_SOLO_PAGINA_1:
            print(f"[=] {operacion}/{tipo}/{slug}: ya existe checkpoint, se omite.")
            continue
        filas, rep = scrapear_distrito(session, operacion, tipo, slug, provincia)
        reportes.append(rep)
        if filas and not VALIDAR_SOLO_PAGINA_1:
            pd.DataFrame(filas).to_csv(ckpt, index=False, encoding="utf-8-sig", sep="|")
        time.sleep(random.uniform(PAUSA_MIN_SEG * 2, PAUSA_MAX_SEG * 2))

    if reportes:
        rep_df = pd.DataFrame(reportes)
        rep_path = OUT_DIR / "reporte_validacion.csv"
        rep_df.to_csv(rep_path, index=False, encoding="utf-8-sig")
        print("\n" + rep_df.to_string(index=False))
        print(f"\n[OK] Reporte de validación -> {rep_path}")
    if VALIDAR_SOLO_PAGINA_1:
        print("\n[i] Modo validación (VALIDAR_SOLO_PAGINA_1 = True): solo se leyó la página 1 "
              "y NO se generan los datos.\n    Pon VALIDAR_SOLO_PAGINA_1 = False y vuelve a "
              "ejecutar para la descarga completa.")


def consolidar():
    archivos = sorted(CHECKPOINT_DIR.glob("*.csv"))
    if not archivos:
        print("No hay checkpoints que consolidar.")
        return None
    df = pd.concat((pd.read_csv(a, sep="|", encoding="utf-8-sig") for a in archivos),
                   ignore_index=True)
    # Un mismo aviso puede aparecer en venta y en alquiler: se deduplica por
    # (posting_id, operacion), no solo por posting_id.
    df = df.drop_duplicates(subset=["posting_id", "operacion"])

    # Coordenadas repetidas -> probable ubicación aproximada (centroide de zona).
    # Se cuentan avisos distintos, para no inflar con los que están en ambas operaciones.
    df["coord_repetida"] = df.groupby(["lat", "lng"])["posting_id"].transform("nunique")
    df.loc[df["lat"].isna(), "coord_repetida"] = pd.NA

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig", sep="|")
    print(f"\n[OK] {len(df)} registros -> {OUTPUT_CSV}")
    print(df.groupby(["operacion", "tipo_busqueda"]).size().rename("avisos").to_string())
    print(f"     Con coordenadas: {df['lat'].notna().sum()} | "
          f"en coordenadas compartidas por >5 avisos: {(df['coord_repetida'] > 5).sum()}")
    sin_op = (~df["operacion_confirmada"].astype(bool)).sum()
    if sin_op:
        print(f"     [!] {sin_op} avisos sin la operación buscada en priceOperationTypes "
              "(revisa el nombre que usa Urbania en operationType)")

    try:
        import geopandas as gpd
        geo = df.dropna(subset=["lat", "lng"])
        gdf = gpd.GeoDataFrame(geo, geometry=gpd.points_from_xy(geo["lng"], geo["lat"]),
                               crs="EPSG:4326")
        # Una capa por operación/tipo: precios de venta y alquiler no son comparables
        for (op, tp), sub in gdf.groupby(["operacion", "tipo_busqueda"]):
            sub.to_file(OUTPUT_GPKG, layer=f"{op}_{tp}", driver="GPKG")
        print(f"     GeoPackage -> {OUTPUT_GPKG} (capas por operación)")
    except ImportError:
        print("     (geopandas no instalado: se omite el GeoPackage)")
    return df


if __name__ == "__main__":
    scrapear()
    if not VALIDAR_SOLO_PAGINA_1:
        consolidar()
