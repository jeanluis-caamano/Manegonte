#!/usr/bin/env python3
"""Recolecta los informes diarios de INDOMET "Temperaturas extremas y precipitaciones".

Cada ejecución:
  1. Pide a INDOMET la lista de todos los informes publicados.
  2. Descarga los que todavía no están guardados y lee su tabla.
  3. Guarda las filas válidas en data/observaciones.csv, bajo la fecha impresa en el informe.
  4. Si un informe no se puede leer con seguridad, no guarda nada de él y termina con error,
     para que GitHub envíe un aviso por correo.

Si el sitio de INDOMET no responde, no hay aviso: se reintenta en la próxima corrida, y si
la caída dura más de 3 días, la revisión de atraso avisa.

Los documentos de la lista que no traen la tabla diaria (resúmenes mensuales, boletines de
pronóstico) se marcan como "ignorado" y no generan aviso.

Con --revisar-atraso, además termina con error si el último día guardado tiene más de 3 días.
"""
import csv
import datetime as dt
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pypdf

RAIZ = Path(__file__).resolve().parent.parent
OBSERVACIONES = RAIZ / "data" / "observaciones.csv"
INFORMES = RAIZ / "data" / "informes.csv"
ESTADO = RAIZ / "data" / "estado.json"

LISTA_URL = (
    "https://indomet.gob.do/wp-json/wp/v2/media?search=temperaturas-extremas"
    "&per_page=100&page={}&orderby=date&order=asc&_fields=id,date_gmt,source_url"
)
HORA_RD = dt.timezone(dt.timedelta(hours=-4))  # República Dominicana: UTC-4 todo el año
DIAS_MAX_ATRASO = 3

COLUMNAS_OBS = ["fecha", "num", "estacion", "provincia", "localidad", "tipo", "tmax", "tmin", "lluvia", "informe"]
COLUMNAS_INF = ["informe", "publicado_utc", "fecha_datos", "filas", "resultado", "detalle"]

MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6, "JULIO": 7,
    "AGOSTO": 8, "SEPTIEMBRE": 9, "SETIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}
PROVINCIAS = [
    "AZUA", "BAHORUCO", "BARAHONA", "DAJABÓN", "DISTRITO NACIONAL", "DUARTE", "EL SEIBO", "ELÍAS PIÑA",
    "ESPAILLAT", "HATO MAYOR", "HERMANAS MIRABAL", "INDEPENDENCIA", "LA ALTAGRACIA", "LA ROMANA", "LA VEGA",
    "MARÍA TRINIDAD SÁNCHEZ", "MONSEÑOR NOUEL", "MONTE CRISTI", "MONTE PLATA", "PEDERNALES", "PERAVIA",
    "PUERTO PLATA", "SAMANÁ", "SAN CRISTÓBAL", "SAN JOSÉ DE OCOA", "SAN JUAN", "SAN PEDRO DE MACORÍS",
    "SÁNCHEZ RAMÍREZ", "SANTIAGO", "SANTIAGO RODRÍGUEZ", "SANTO DOMINGO", "VALVERDE",
]
RE_FECHA = re.compile(r"^\s*(\d{1,2})\s+DE\s+([A-ZÁÉÍÓÚ]+)\s+(?:DEL?\s+)?(\d{4})\s*$")
RE_FILA = re.compile(r"^\s*(\d{1,3})\.?\s{2,}\S")
RE_VALOR = re.compile(r"^(-?\d+(?:\.\d+)?|///)$")
RE_TIPO = re.compile(r"^EM[A-Z]{1,3}$")
RE_TROZO = re.compile(r"\S+(?: \S+)*")

# Etiquetas de la cabecera de la tabla y el nombre de columna que les corresponde.
CABECERA_TEXTO = [("ESTACI", "estacion"), ("PROVINCIA", "provincia"), ("LOCALIDAD", "localidad"), ("TIPO", "tipo")]
CABECERA_NUMERO = [("T. MÁX", "tmax"), ("T. MÍN", "tmin"), ("LLUVIA", "lluvia")]


class InformeIlegible(Exception):
    """El informe no tiene la forma esperada; no se guarda nada de él."""


class NoEsInformeDiario(Exception):
    """El documento no trae la tabla diaria (por ejemplo, un resumen mensual)."""


def descargar(url, intentos=3):
    for n in range(intentos):
        r = subprocess.run(
            ["curl", "-sfL", "--max-time", "90", "-A", "Mozilla/5.0 (Manegonte)", url],
            capture_output=True,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
        time.sleep(5 * (n + 1))
    raise ConnectionError(f"No se pudo descargar {url}")


def lista_de_informes():
    informes, pagina = [], 1
    while True:
        cuerpo = json.loads(descargar(LISTA_URL.format(pagina)))
        if not isinstance(cuerpo, list) or not cuerpo:
            break
        informes += [i for i in cuerpo if i["source_url"].lower().endswith(".pdf")]
        if len(cuerpo) < 100:
            break
        pagina += 1
    return informes


def leer_cabecera(linea):
    """Posición de cada columna en la línea de cabecera, o None si la línea no es una cabecera."""
    if "ESTACI" not in linea or "LLUVIA" not in linea:
        return None
    texto = {col: linea.find(et) for et, col in CABECERA_TEXTO if et in linea}
    numero = {}
    for et, col in CABECERA_NUMERO:
        i = linea.find(et)
        if i >= 0:
            fin = linea.find(")", i)
            numero[col] = (i + (fin if fin > i else i + len(et))) / 2  # centro de la etiqueta
    return {"texto": texto, "numero": numero}


def separar_provincia(trozo):
    """'JARDÍN BOTÁNICO SANTIAGO (EMSANTIAGO' -> ('JARDÍN BOTÁNICO SANTIAGO (EM', 'SANTIAGO')."""
    for p in sorted(PROVINCIAS, key=len, reverse=True):
        if trozo.endswith(p) and len(trozo) > len(p):
            return trozo[: -len(p)].rstrip(), p
    return None


def leer_fila(linea, cab):
    fila = {"estacion": "", "provincia": "", "localidad": "", "tipo": "", "tmax": "", "tmin": "", "lluvia": ""}
    trozos = [(m.start(), m.end(), m.group()) for m in RE_TROZO.finditer(linea)]
    num = trozos.pop(0)[2].rstrip(".")
    texto = sorted(cab["texto"].items(), key=lambda x: x[1])
    for inicio, fin, t in trozos:
        if RE_VALOR.match(t) and cab["numero"]:
            centro = (inicio + fin) / 2
            col = min(cab["numero"], key=lambda c: abs(cab["numero"][c] - centro))
        elif RE_TIPO.match(t):
            col = "tipo"
        else:
            previas = [c for c, pos in texto if pos <= inicio + 3]
            if not previas:
                raise InformeIlegible(f"texto fuera de columna: {linea.strip()!r}")
            col = previas[-1]
            siguiente = next((pos for c, pos in texto if pos > inicio + 3), None)
            if col == "estacion" and siguiente is not None and fin > siguiente + 1:
                partes = separar_provincia(t)
                if not partes:
                    raise InformeIlegible(f"nombre de estación montado sobre otra columna: {linea.strip()!r}")
                fila["estacion"], fila["provincia"] = partes
                continue
        if fila[col] and col in ("estacion", "localidad"):
            fila[col] += " " + t  # un nombre con doble espacio dentro, p. ej. "BARRAQUITO  (EMA)"
            continue
        if fila[col]:
            raise InformeIlegible(f"dos valores en la columna {col}: {linea.strip()!r}")
        fila[col] = "" if t == "///" else t
    if not any(fila[c] for c in ("tipo", "tmax", "tmin", "lluvia")) and not any(RE_VALOR.match(t) for _, _, t in trozos):
        return None, None  # no es una fila de datos (p. ej. el número de página al pie)
    if not fila["estacion"]:
        raise InformeIlegible(f"fila sin nombre de estación: {linea.strip()!r}")
    return num.zfill(3), fila


def leer_informe(pdf_bytes):
    """Devuelve (fecha, filas). Lanza NoEsInformeDiario o InformeIlegible."""
    lector = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    fecha, filas, vistas, vio_cabecera, resumen = None, [], set(), False, False
    for pagina in lector.pages:
        cab = None
        for linea in pagina.extract_text(extraction_mode="layout").splitlines():
            if "COMPORTAMIENTO" in linea.upper():
                resumen = True
            nueva = leer_cabecera(linea.upper())
            if nueva:
                cab, vio_cabecera = nueva, True
                continue
            m = RE_FECHA.match(linea.upper())
            if m:
                mes = MESES.get(m[2])
                if not mes:
                    raise InformeIlegible(f"mes desconocido en la fecha: {linea.strip()!r}")
                f = dt.date(int(m[3]), mes, int(m[1]))
                if fecha and f != fecha:
                    raise InformeIlegible(f"el informe trae dos fechas: {fecha} y {f}")
                fecha = f
                continue
            if cab is None or not RE_FILA.match(linea):
                continue
            num, fila = leer_fila(linea, cab)
            if num is None:
                continue
            # INDOMET a veces repite un número de fila por error; lo que no puede repetirse es la estación.
            clave = (fila["estacion"], fila["tipo"])
            if clave in vistas:
                raise InformeIlegible(f"la estación {fila['estacion']} aparece dos veces")
            vistas.add(clave)
            filas.append(dict(fila, num=num))
    if not vio_cabecera or not filas or (resumen and not fecha):
        raise NoEsInformeDiario("el documento no trae la tabla diaria")
    if not fecha:
        raise InformeIlegible("no se encontró la fecha dentro del informe")
    if len(filas) < 20:
        raise InformeIlegible(f"solo se encontraron {len(filas)} estaciones")
    return fecha, filas


def leer_csv(ruta):
    if not ruta.exists():
        return []
    with ruta.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def escribir_csv(ruta, columnas, filas):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas)
        w.writeheader()
        w.writerows(filas)


def main():
    revisar_atraso = "--revisar-atraso" in sys.argv
    observaciones = leer_csv(OBSERVACIONES)
    registro = {r["informe"]: r for r in leer_csv(INFORMES)}
    ya_rechazados = {u for u, r in registro.items() if r["resultado"] == "rechazado"}
    errores = []

    try:
        lista = lista_de_informes()
    except ConnectionError as e:
        print("AVISO: INDOMET no respondió; se reintentará en la próxima corrida.", e, flush=True)
        lista = None
    pendientes = [i for i in lista or []
                  if registro.get(i["source_url"], {}).get("resultado") not in ("guardado", "ignorado")]
    # Del más antiguo al más reciente: si INDOMET publica una corrección del mismo día, gana la última.
    pendientes.sort(key=lambda i: i["date_gmt"])
    por_fecha = {}
    for o in observaciones:
        por_fecha.setdefault(o["fecha"], []).append(o)

    for inf in pendientes:
        url = inf["source_url"]
        entrada = {"informe": url, "publicado_utc": inf["date_gmt"], "fecha_datos": "", "filas": 0}
        try:
            fecha, filas = leer_informe(descargar(url))
        except ConnectionError as e:
            # INDOMET no entregó el PDF: no se anota, así se reintenta en la próxima corrida.
            print("AVISO: INDOMET no respondió; se reintentará en la próxima corrida.", e, flush=True)
            continue
        except NoEsInformeDiario as e:
            entrada.update(resultado="ignorado", detalle=str(e))
        except (InformeIlegible, pypdf.errors.PyPdfError) as e:
            entrada.update(resultado="rechazado", detalle=str(e))
            if url not in ya_rechazados:
                errores.append(f"{url}: {e}")
        else:
            por_fecha[fecha.isoformat()] = [dict(f, fecha=fecha.isoformat(), informe=url) for f in filas]
            entrada.update(fecha_datos=fecha.isoformat(), filas=len(filas), resultado="guardado", detalle="")
            print(f"guardado {fecha} ({len(filas)} estaciones) desde {url}", flush=True)
        registro[url] = entrada

    observaciones = [o for f in sorted(por_fecha) for o in sorted(por_fecha[f], key=lambda o: o["num"])]
    ahora = dt.datetime.now(HORA_RD)
    ultimo = max(por_fecha) if por_fecha else None
    # Sin la lista de INDOMET no se revisó nada: los datos y la hora de la última revisión quedan como estaban.
    if lista is not None:
        escribir_csv(OBSERVACIONES, COLUMNAS_OBS, observaciones)
        escribir_csv(INFORMES, COLUMNAS_INF, sorted(registro.values(), key=lambda r: r["publicado_utc"]))
        ESTADO.write_text(json.dumps({
            "actualizado": ahora.strftime("%Y-%m-%dT%H:%M"),
            "ultimo_dia": ultimo,
            "dias": len(por_fecha),
            "filas": len(observaciones),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if revisar_atraso and ultimo:
        atraso = (ahora.date() - dt.date.fromisoformat(ultimo)).days
        if atraso > DIAS_MAX_ATRASO:
            errores.append(f"el último día guardado es {ultimo}, hace {atraso} días; INDOMET no ha publicado nada nuevo")

    for e in errores:
        print("ERROR:", e, file=sys.stderr)
    sys.exit(1 if errores else 0)


if __name__ == "__main__":
    main()
