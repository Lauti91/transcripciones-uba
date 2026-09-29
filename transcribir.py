"""
Transcripciones UBA: Drive -> Groq (Whisper) / Gemini -> Drive

Recorre las materias dentro de la carpeta CLASES de Google Drive (carpetas o
accesos directos). Para cada audio de clase:

  1. Si no tiene transcripción: lo baja, lo parte en tramos de hasta 45 minutos
     con ffmpeg y transcribe cada tramo con Groq (whisper-large-v3). Si Groq
     falla o no tiene cuota, usa gemini-3.5-transcribe como respaldo (acepta
     hasta ~50 min por pedido). Cada tramo terminado se guarda en
     Transcripciones/_partes, así que si una corrida se corta, la siguiente
     retoma desde el tramo que falta. Al final une los tramos, los corrige
     (nombres, términos y ortografía, con el glosario de <materia>/Contexto y
     gpt-oss-120b en Groq; la versión cruda queda en _partes) y guarda la
     transcripción con el mismo nombre que el audio.
  2. Si tiene transcripción pero no resumen: genera un resumen reestructurado
     con un modelo de texto de Gemini y lo guarda como "<nombre> - resumen".

Transcripción y resumen se guardan como Google Docs en la subcarpeta
"Transcripciones" de cada materia.

Fase de pendientes (al final de cada corrida): las transcripciones que quedaron
sin corregir (se acabó la cuota) se corrigen desde el original de _partes, de la
fecha más vieja a la más nueva, y el Doc se reemplaza EN EL LUGAR (mismo archivo).

Variables de entorno necesarias (en GitHub van como secretos):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
  GEMINI_API_KEY, CARPETA_CLASES_ID
Opcionales:
  GROQ_API_KEY (sin ella se transcribe solo con Gemini),
  CARPETA_RESUMENES_SPARK_ID (sin ella se saltea la fase 0),
  PRUEBA_MATERIA + UNO de PRUEBA_GROQ_AUDIO, PRUEBA_CORRECCION_AUDIO o RECORREGIR_AUDIO
  (modos manuales, ver prueba_groq, prueba_correccion y recorregir_clase),
  MAX_PENDIENTES (máximo de clases atrasadas a corregir por corrida).
"""

import csv
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

NOMBRE_TRANSCRIPCIONES = "Transcripciones"
NOMBRE_PARTES = "_partes"
NOMBRE_RESUMENES = "Resúmenes de clase"   # dentro de cada materia (visible para Claude)
SUFIJO_RESUMEN_SPARK = " - Resumen de clase"
SUFIJO_RESUMEN = " - resumen"

MODELO_TRANSCRIPCION = "gemini-3.5-transcribe"   # respaldo si Groq falla

# Transcriptor principal: Groq (API compatible con OpenAI).
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELO_GROQ = "whisper-large-v3"
MAX_BYTES_GROQ = 25 * 1024 * 1024   # límite de archivo de Groq
TIMEOUT_GROQ_S = 300
# Si Groq pide esperar más que esto (ej. límite por hora), se usa Gemini.
MAX_ESPERA_GROQ_S = 180
SUFIJO_PRUEBA_GROQ = " (groq)"

# Corrección posterior a la transcripción (nombres, términos, ortografía),
# con el glosario de <materia>/Contexto/Glosario*. Misma GROQ_API_KEY.
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELO_CORRECCION = "openai/gpt-oss-120b"
NOMBRE_CONTEXTO = "Contexto"
# Free tier: 8.000 tokens/min y 200.000/día. Cada pedido lleva prompt +
# glosario + bloque y devuelve el bloque corregido: con ~1.200 palabras
# entra holgado en 8.000 (si el glosario es largo, el bloque se achica).
LIMITE_TOKENS_MINUTO_CORRECCION = 8000
PALABRAS_POR_BLOQUE = 1200
TOKENS_POR_PALABRA = 1.5   # estimación conservadora para español
# Si el bloque corregido cambia mucho de largo, se descarta (usa el original).
MIN_PROPORCION_CORRECCION = 0.85
MAX_PROPORCION_CORRECCION = 1.15
# Filtro de cada corrección propuesta: en palabras comunes, similitud mínima
# (1 - Levenshtein / largo mayor, sin tildes ni mayúsculas) entre original y
# corrección. Con 0,6 caen "bota → aborto" (0,50) y "dotes → dotaciones"
# (0,50) y pasa "microqueditos → microcréditos" (0,85).
UMBRAL_SIMILITUD = float(os.environ.get("UMBRAL_SIMILITUD", "0.6"))
# Nombres propios y siglas: además de estar en el glosario, el original tiene
# que parecerse (>= 0,4) o figurar en el glosario como variante de ese término.
# Así cae "violera → Duflo" y pasa "Kabir → Kabeer" (0,50, y además variante).
UMBRAL_SIMILITUD_NOMBRES = float(os.environ.get("UMBRAL_SIMILITUD_NOMBRES", "0.4"))
TABLA_GUIONES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-"})
CARPETA_AUDITORIA = "auditoria"   # CSV local del modo prueba (el workflow lo sube como artefacto)
# Glosario por bloque: en cada pedido se manda lo fijo (encabezado, docentes,
# cronograma) y solo las entradas que aparecen o se parecen a algo del bloque.
# Ahorra tokens del límite diario (un glosario de ~6.000 tokens se reenviaba
# entero en cada bloque). El filtro de correcciones sigue usando el glosario
# completo, así que lo que no se manda no se acepta por error.
# (no "catedra": hay secciones de conceptos cuyo título menciona "la cátedra")
SECCIONES_FIJAS_GLOSARIO = ("docente", "profesor", "cronograma", "calendario")
UMBRAL_RELEVANCIA_GLOSARIO = float(os.environ.get("UMBRAL_RELEVANCIA_GLOSARIO", "0.55"))
MAX_CARACTERES_GLOSARIO_VARIABLE = 4200    # ≈ 1.200 tokens de entradas por bloque, como máximo
PALABRAS_VACIAS_GLOSARIO = {
    "que", "los", "las", "del", "con", "por", "para", "una", "uno", "sus", "como", "este", "esta", "esto",
    "the", "and", "for", "with", "sin", "sobre", "entre", "desde", "hasta", "cuando", "donde",
}
# Fase de pendientes: estimación de tiempo por bloque (incluye la espera por
# tokens/min), para no empezar una clase que no alcanza a terminar en la corrida.
MINUTOS_POR_BLOQUE_CORRECCION = 0.8
SUFIJO_SIN_CORREGIR = " (sin corregir)"    # copia cruda, en _partes
SUFIJO_PRUEBA_CORRECCION = " (corregida)"
SEPARADOR_CORRECCIONES = "────────────────────"
TITULO_CORRECCIONES = "Correcciones aplicadas"

PROMPT_CORRECCION = """Sos corrector de transcripciones automáticas de clases universitarias de economía (FCE-UBA), grabadas en español rioplatense. Recibís UN bloque de una transcripción hecha con Whisper, el glosario de la materia y el tema de la clase.

Tu tarea es SOLO corregir errores de transcripción:
- Nombres propios (docentes, autores, instituciones) y términos técnicos mal transcriptos, cuando haya evidencia: que figuren en el glosario (incluidas las variantes erróneas entre paréntesis) o que el contexto inmediato lo deje claro.
- Ortografía evidente (tildes, letras cambiadas) y puntuación mínima necesaria.

Prohibido:
- Resumir, acortar, reordenar, agregar contenido o explicaciones.
- "Arreglar" frases ininteligibles o cortadas inventándoles sentido: dejalas exactamente como están.
- Cambiar el registro oral o el voseo rioplatense (muletillas, repeticiones y frases coloquiales quedan como están).
- Traducir términos: si se dijo en inglés, queda en inglés (ej. "Behavioral Economics" queda igual).
- Expandir abreviaturas o siglas, o completar palabras cortadas (ej. "pol" queda "pol"; "PBG" queda "PBG").
- Completar o alargar nombres (ej. "David" NO pasa a "David Weil").
- Cambiar números o su formato (ej. "3" NO pasa a "tres", ni al revés).
- Cambiar apodos o formas de trato que figuran en el glosario (ej. "Luz" queda "Luz").
- Corregir un nombre propio o sigla si la forma corregida no está escrita en el glosario.
- Corregir por adivinanza: si no hay evidencia clara, no toques la palabra.

Ante la duda, dejá el original.

Respondé SOLO con un objeto JSON con dos campos:
{"texto": "<el bloque completo, corregido>", "correcciones": [{"original": "<como estaba>", "corregido": "<como quedó>"}]}
Si no hay nada que corregir, devolvé el bloque idéntico y "correcciones": []. En "correcciones" poné cada cambio una sola vez, con la palabra o frase corta afectada (no oraciones enteras)."""
# Para el resumen: si uno está saturado, se prueba el siguiente.
MODELOS_RESUMEN = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]

# Máximo por tramo: 45 min de audio ≈ 86.400 tokens (32 por segundo), bajo el
# límite de 98.304 del modelo de transcripción. La clase se reparte en tramos
# IGUALES (ej. 85 min -> 2 de 42,5), para no gastar un pedido en un resto corto:
# la cuota diaria de pedidos es el cuello de botella.
SEGUNDOS_POR_TRAMO = 45 * 60

# Reintentos ante saturación (503) o límites (429): espera creciente.
# Pocos a propósito: cada reintento gasta cuota diaria, y el workflow
# corre cada 30 min, así que es mejor insistir poco y repartir los
# intentos entre corridas que agotar la cuota en una sola.
MAX_REINTENTOS = 3
ESPERA_INICIAL_S = 30
ESPERA_MAXIMA_S = 120

# Tiempo máximo de trabajo por corrida. Pasado esto no se empieza nada
# nuevo; lo pendiente sigue en la próxima corrida.
MAX_MINUTOS_CORRIDA = 45

MIN_CARACTERES = 200

# Reintentos automáticos de las llamadas a Drive ante cortes de conexión.
REINTENTOS_DRIVE = 5

# Tiempo máximo de espera por pedido a Gemini. Si se cumple, se trata como
# saturación y se reintenta (un tramo de 40 min suele tardar 1-2 min).
TIMEOUT_GEMINI_S = 300

# Pausa entre tramos consecutivos, para no superar el límite de tokens por minuto.
PAUSA_ENTRE_TRAMOS_S = 60

# Los resúmenes los genera una tarea programada de Gemini (Spark) a partir de
# las transcripciones. Con False, este script solo transcribe. Se puede
# forzar con la variable de entorno HACER_RESUMENES=true.
HACER_RESUMENES = os.environ.get("HACER_RESUMENES", "false").lower() == "true"

EXTENSIONES_AUDIO = {"m4a", "mp3", "wav", "ogg", "oga", "opus", "aac", "flac", "amr", "webm", "3gp", "mp4"}

PROMPT_TRANSCRIBIR = (
    "Transcribí este audio de una clase universitaria de economía, en español. "
    "Devolvé únicamente la transcripción literal, en texto plano, sin comentarios ni resúmenes."
)

PROMPT_RESUMEN = """Te paso la transcripción literal de una clase universitaria de economía (materia: {materia}, clase: {clase}).

Armá un resumen reestructurado de la clase, pensado para estudiar. Reglas:
- Usá solo lo que está en la transcripción; no agregues contenido externo. Si algo es confuso o se corta, indicalo.
- Ordená el contenido por temas, no por el orden en que se dijo.
- Conservá definiciones, fórmulas, supuestos, ejemplos y los avisos de la cátedra (fechas, parciales, trabajos prácticos).
- Español, texto plano con títulos en mayúsculas y viñetas con "-" (sin Markdown complejo).

Formato:

MATERIA: {materia}
CLASE: {clase}
DOCENTE: (si se menciona; si no, "no identificado")
TEMAS: tema 1; tema 2; ...

AVISOS DE LA CÁTEDRA
- ...

DESARROLLO POR TEMA
TEMA 1: ...
- ...

CONCEPTOS Y DEFINICIONES CLAVE
- ...

FÓRMULAS Y EXPRESIONES (si las hay)
- ...

DUDAS O PARTES POCO CLARAS DE LA GRABACIÓN (si las hay)
- ...

TRANSCRIPCIÓN:
---
{transcripcion}
---
"""

GEMINI_BASE = "https://generativelanguage.googleapis.com"
INICIO = time.time()
GLOSARIOS = {}   # carpeta de materia -> texto del glosario (o None)
MIME_DOC = "application/vnd.google-apps.document"
USO_CORRECCION = {"prompt": 0, "completion": 0, "pedidos": 0}   # tokens reales que informa Groq, acumulados en la corrida
CORREGIDOS_EN_ESTA_CORRIDA = set()   # (materia, nombre del Doc) ya corregidos completos: la fase de pendientes no los relee
ESTADO = {"sin_cuota_gemini": False, "sin_cuota_groq": False, "sin_cuota_correccion": False}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tiempo_agotado():
    return (time.time() - INICIO) / 60 > MAX_MINUTOS_CORRIDA


def env(nombre):
    valor = os.environ.get(nombre)
    if not valor:
        sys.exit(f"Falta la variable de entorno {nombre}.")
    return valor


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def conectar_drive():
    creds = Credentials(
        token=None,
        refresh_token=env("GOOGLE_REFRESH_TOKEN"),
        client_id=env("GOOGLE_CLIENT_ID"),
        client_secret=env("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def listar(drive, carpeta_id):
    """Todos los elementos (no borrados) dentro de una carpeta."""
    items, token = [], None
    while True:
        r = drive.files().list(
            q=f"'{carpeta_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, shortcutDetails)",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=REINTENTOS_DRIVE)
        items += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return items


def obtener_materias(drive, clases_id):
    """[(nombre, id_carpeta)] de cada materia, resolviendo accesos directos."""
    materias = []
    for item in listar(drive, clases_id):
        if item["mimeType"] == "application/vnd.google-apps.folder":
            materias.append((item["name"], item["id"]))
        elif item["mimeType"] == "application/vnd.google-apps.shortcut":
            det = item.get("shortcutDetails", {})
            if det.get("targetMimeType") == "application/vnd.google-apps.folder":
                materias.append((item["name"], det["targetId"]))
    return materias


def subcarpeta(drive, padre_id, nombre):
    for item in listar(drive, padre_id):
        if item["mimeType"] == "application/vnd.google-apps.folder" and item["name"] == nombre:
            return item["id"]
    log(f"Creando carpeta '{nombre}'...")
    nueva = drive.files().create(
        body={"name": nombre, "mimeType": "application/vnd.google-apps.folder", "parents": [padre_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute(num_retries=REINTENTOS_DRIVE)
    return nueva["id"]


def archivos_por_nombre(drive, carpeta_id):
    return {i["name"]: i for i in listar(drive, carpeta_id) if i["mimeType"] != "application/vnd.google-apps.folder"}


def bajar(drive, archivo_id, destino):
    with open(destino, "wb") as f:
        req = drive.files().get_media(fileId=archivo_id, supportsAllDrives=True)
        dl = MediaIoBaseDownload(f, req, chunksize=32 * 1024 * 1024)
        terminado = False
        while not terminado:
            _, terminado = dl.next_chunk(num_retries=REINTENTOS_DRIVE)


def leer_texto(drive, item):
    """Lee el texto de un archivo de texto plano o de un Google Doc."""
    if item["mimeType"] == "application/vnd.google-apps.document":
        req = drive.files().export_media(fileId=item["id"], mimeType="text/plain")
    else:
        req = drive.files().get_media(fileId=item["id"], supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    terminado = False
    while not terminado:
        _, terminado = dl.next_chunk(num_retries=REINTENTOS_DRIVE)
    # Google exporta los Docs con \r\n: se normaliza para que el texto no dependa de eso.
    return buf.getvalue().decode("utf-8-sig").replace("\r\n", "\n")


def guardar_texto(drive, carpeta_id, nombre, texto, como_doc=False, mime="text/plain"):
    """Guarda un texto en Drive; con como_doc=True lo convierte en Google Doc."""
    media = MediaIoBaseUpload(io.BytesIO(texto.encode("utf-8")), mimetype=mime, resumable=True)
    tipo = "application/vnd.google-apps.document" if como_doc else mime
    drive.files().create(
        body={"name": nombre, "parents": [carpeta_id], "mimeType": tipo},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute(num_retries=REINTENTOS_DRIVE)


def a_papelera(drive, archivo_id):
    drive.files().update(fileId=archivo_id, body={"trashed": True}, supportsAllDrives=True).execute(num_retries=REINTENTOS_DRIVE)


def es_audio(item):
    if item["mimeType"].startswith("audio/"):
        return True
    ext = item["name"].rsplit(".", 1)[-1].lower() if "." in item["name"] else ""
    return ext in EXTENSIONES_AUDIO


def nombre_base(nombre_audio):
    return nombre_audio.rsplit(".", 1)[0] if "." in nombre_audio else nombre_audio


# ---------------------------------------------------------------------------
# Audio (ffmpeg)
# ---------------------------------------------------------------------------

def duracion_segundos(ruta):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(ruta)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def extraer_tramo(ruta, desde, duracion, destino):
    """Recorta un tramo y lo convierte a MP3 mono liviano (alcanza para voz)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(desde), "-t", str(duracion), "-i", str(ruta),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(destino)],
        check=True,
    )


# ---------------------------------------------------------------------------
# Gemini (REST)
# ---------------------------------------------------------------------------

def con_reintentos(descripcion, funcion):
    """Ejecuta funcion(); ante 429/5xx espera y reintenta con espera creciente."""
    espera = ESPERA_INICIAL_S
    for intento in range(1, MAX_REINTENTOS + 1):
        try:
            return funcion()
        except ErrorReintentable as e:
            if intento == MAX_REINTENTOS or tiempo_agotado():
                raise
            pausa = max(espera, math.ceil(e.espera) + 5) if e.espera else espera
            log(f"{descripcion}: {e} -> reintento {intento}/{MAX_REINTENTOS - 1} en {pausa}s")
            time.sleep(pausa)
            espera = min(espera * 2, ESPERA_MAXIMA_S)


class ErrorReintentable(Exception):
    """Error transitorio. 'espera' = segundos sugeridos por la API antes de reintentar."""

    def __init__(self, mensaje, espera=None):
        super().__init__(mensaje)
        self.espera = espera


def analizar_429(r):
    """Devuelve (límites violados, segundos sugeridos de espera) de un 429 de Gemini.

    Un 429 puede listar varios límites a la vez (por minuto y por día), así
    que solo se considera "cuota diaria agotada" si TODOS son diarios.
    """
    limites, espera = [], None
    try:
        for d in r.json().get("error", {}).get("details", []):
            tipo = d.get("@type", "")
            if tipo.endswith("QuotaFailure"):
                limites += [v.get("quotaId", "") for v in d.get("violations", [])]
            elif tipo.endswith("RetryInfo"):
                espera = float(str(d.get("retryDelay", "0s")).rstrip("s") or 0)
    except ValueError:
        pass
    return sorted(set(q for q in limites if q)), espera


class CuotaDiariaAgotada(Exception):
    """Se terminó la cuota diaria de Gemini: no tiene sentido seguir hoy."""


def revisar_respuesta(r, contexto):
    if r.status_code == 200:
        return
    detalle = r.text[:500]
    if r.status_code == 429:
        limites, espera = analizar_429(r)
        if limites and all("PerDay" in q for q in limites):
            raise CuotaDiariaAgotada(f"{contexto}: cuota diaria de Gemini agotada ({', '.join(limites)}).")
        raise ErrorReintentable(f"respondió 429 ({', '.join(limites) or 'límite de uso'})", espera=espera)
    if r.status_code >= 500:
        raise ErrorReintentable(f"respondió {r.status_code}")
    raise RuntimeError(f"{contexto} respondió {r.status_code}: {detalle}")


def subir_a_gemini(api_key, ruta, mime="audio/mpeg"):
    tamano = os.path.getsize(ruta)

    def _subir():
        inicio = requests.post(
            f"{GEMINI_BASE}/upload/v1beta/files?key={api_key}",
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(tamano),
                "X-Goog-Upload-Header-Content-Type": mime,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": Path(ruta).name}},
            timeout=60,
        )
        revisar_respuesta(inicio, "Inicio de subida")
        url = inicio.headers.get("X-Goog-Upload-URL") or inicio.headers.get("x-goog-upload-url")
        with open(ruta, "rb") as f:
            subida = requests.post(
                url,
                headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
                data=f,
                timeout=600,
            )
        revisar_respuesta(subida, "Subida")
        return subida.json()["file"]

    archivo = con_reintentos("Subida a Gemini", _subir)

    # Esperar a que termine de procesarse
    for _ in range(60):
        info = requests.get(f"{GEMINI_BASE}/v1beta/{archivo['name']}?key={api_key}", timeout=60).json()
        if info.get("state") == "ACTIVE":
            return info
        if info.get("state") == "FAILED":
            raise RuntimeError(f"Gemini no pudo procesar el archivo: {info}")
        time.sleep(5)
    raise RuntimeError("El archivo no terminó de procesarse en Gemini.")


def borrar_de_gemini(api_key, nombre):
    try:
        requests.delete(f"{GEMINI_BASE}/v1beta/{nombre}?key={api_key}", timeout=60)
    except requests.RequestException:
        pass


def extraer_texto(json_resp):
    candidato = (json_resp.get("candidates") or [{}])[0]
    partes = (candidato.get("content") or {}).get("parts") or []
    # Los modelos generales devuelven "text"; el de transcripción, "audioTranscription.text".
    texto = "".join(p.get("text") or (p.get("audioTranscription") or {}).get("text") or "" for p in partes)
    return texto, candidato.get("finishReason")


def generar(api_key, modelo, parts, contexto):
    def _llamar():
        try:
            r = requests.post(
                f"{GEMINI_BASE}/v1beta/models/{modelo}:generateContent?key={api_key}",
                json={"contents": [{"parts": parts}]},
                timeout=TIMEOUT_GEMINI_S,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ErrorReintentable(f"sin respuesta ({type(e).__name__})") from e
        revisar_respuesta(r, f"{modelo} ({contexto})")
        texto, fin = extraer_texto(r.json())
        if len(texto.strip()) < MIN_CARACTERES:
            # Respuesta vacía: suele ser algo transitorio, se reintenta.
            raise ErrorReintentable(f"devolvió {len(texto.strip())} caracteres (finishReason: {fin})")
        if fin == "MAX_TOKENS":
            log(f"AVISO: {modelo} cortó la salida por límite de tokens ({contexto}); puede estar incompleta.")
        return texto

    return con_reintentos(f"{modelo} ({contexto})", _llamar)


# ---------------------------------------------------------------------------
# Groq (Whisper)
# ---------------------------------------------------------------------------

class SinCuotaGroq(Exception):
    """Groq agotó su cuota (diaria, o por hora con espera larga): usar Gemini."""


def segundos_espera_groq(r):
    """Segundos que Groq pide esperar (header retry-after o texto 'try again in 1m2.5s')."""
    try:
        return float(r.headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    try:
        mensaje = r.json().get("error", {}).get("message", "")
    except ValueError:
        return None
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", mensaje)
    if not m or not any(m.groups()):
        return None
    h, mi, se = (float(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def revisar_respuesta_groq(r, contexto):
    if r.status_code == 200:
        return
    if r.status_code == 429:
        try:
            mensaje = r.json().get("error", {}).get("message", "")
        except ValueError:
            mensaje = r.text[:300]
        espera = segundos_espera_groq(r)
        if "per day" in mensaje.lower() or "(ASPD)" in mensaje or "(RPD)" in mensaje:
            raise SinCuotaGroq(f"{contexto}: cuota diaria de Groq agotada.")
        if espera is not None and espera > MAX_ESPERA_GROQ_S:
            raise SinCuotaGroq(f"{contexto}: Groq pide esperar {espera / 60:.0f} min (límite de uso).")
        raise ErrorReintentable("respondió 429 (límite de uso)", espera=espera)
    if r.status_code >= 500:
        raise ErrorReintentable(f"respondió {r.status_code}")
    raise RuntimeError(f"{contexto} respondió {r.status_code}: {r.text[:500]}")


def quitar_repeticiones(texto, minimo=4):
    """Whisper a veces repite la misma frase en silencios largos: deja una sola.

    Solo colapsa frases idénticas consecutivas repetidas 'minimo' veces o más.
    Devuelve (texto, cantidad de frases quitadas).
    """
    frases = re.split(r"(?<=[.!?…])\s+", texto.strip())
    salida, quitadas, i = [], 0, 0
    while i < len(frases):
        j = i
        while j + 1 < len(frases) and frases[j + 1].strip().lower() == frases[i].strip().lower():
            j += 1
        repeticiones = j - i + 1
        salida.append(frases[i])
        if repeticiones < minimo:
            salida += frases[i + 1:j + 1]
        else:
            quitadas += repeticiones - 1
        i = j + 1
    return " ".join(salida), quitadas


def transcribir_con_groq(groq_key, ruta, contexto):
    tamano = os.path.getsize(ruta)
    if tamano > MAX_BYTES_GROQ:
        raise RuntimeError(f"{contexto}: el tramo pesa {tamano / 1024 / 1024:.1f} MB (Groq acepta hasta 25 MB).")

    def _llamar():
        try:
            with open(ruta, "rb") as f:
                r = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {groq_key}"},
                    files={"file": (Path(ruta).name, f, "audio/mpeg")},
                    data={"model": MODELO_GROQ, "language": "es", "response_format": "text", "temperature": "0"},
                    timeout=TIMEOUT_GROQ_S,
                )
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ErrorReintentable(f"sin respuesta ({type(e).__name__})") from e
        revisar_respuesta_groq(r, f"Groq ({contexto})")
        texto = r.text.strip()
        if len(texto) < MIN_CARACTERES:
            raise ErrorReintentable(f"devolvió {len(texto)} caracteres")
        texto, quitadas = quitar_repeticiones(texto)
        if quitadas:
            log(f"AVISO: Groq repitió frases ({contexto}); quité {quitadas} repetición(es).")
        return texto

    return con_reintentos(f"Groq ({contexto})", _llamar)


def transcribir_con_gemini(api_key, ruta, contexto):
    archivo_gemini = subir_a_gemini(api_key, ruta)
    try:
        return generar(
            api_key,
            MODELO_TRANSCRIPCION,
            [{"text": PROMPT_TRANSCRIBIR},
             {"file_data": {"mime_type": archivo_gemini["mimeType"], "file_uri": archivo_gemini["uri"]}}],
            contexto,
        )
    finally:
        borrar_de_gemini(api_key, archivo_gemini["name"])


def transcribir_tramo(claves, ruta, contexto, solo_groq=False):
    """Transcribe un tramo con Groq y, si falla, con Gemini. Devuelve (texto, motor)."""
    groq_key = claves.get("groq")
    if groq_key and not ESTADO["sin_cuota_groq"]:
        try:
            return transcribir_con_groq(groq_key, ruta, contexto), "groq"
        except SinCuotaGroq as e:
            ESTADO["sin_cuota_groq"] = True
            if solo_groq:
                raise
            log(f"{e} Sigo con Gemini en esta corrida.")
        except Exception as e:  # noqa: BLE001
            if solo_groq:
                raise
            log(f"Groq falló ({contexto}): {e}. Pruebo con Gemini.")
    elif solo_groq:
        raise RuntimeError("Modo de prueba: falta GROQ_API_KEY o Groq no tiene cuota.")

    if ESTADO["sin_cuota_gemini"]:
        raise CuotaDiariaAgotada(f"{contexto}: Groq no disponible y cuota diaria de Gemini agotada.")
    try:
        return transcribir_con_gemini(claves["gemini"], ruta, contexto), "gemini"
    except CuotaDiariaAgotada:
        ESTADO["sin_cuota_gemini"] = True
        raise


# ---------------------------------------------------------------------------
# Corrección de la transcripción (Groq chat, con glosario de la materia)
# ---------------------------------------------------------------------------

class SinCuotaCorreccion(Exception):
    """El modelo de corrección agotó su cuota diaria: se guarda sin corregir."""


class BloqueDescartado(Exception):
    """La respuesta del corrector no es usable para ese bloque (se usa el original)."""

    def __init__(self, mensaje, propuestas=None):
        super().__init__(mensaje)
        self.propuestas = propuestas or []


def segundos_de_duracion(valor):
    """'1m2.5s', '7.66s', '150ms', '2' -> segundos (None si no se entiende)."""
    if valor is None:
        return None
    valor = str(valor).strip()
    try:
        return float(valor)
    except ValueError:
        pass
    m = re.fullmatch(r"(?:([\d.]+)h)?(?:([\d.]+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?", valor)
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = (float(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def dividir_en_bloques(texto, palabras=PALABRAS_POR_BLOQUE):
    """Parte el texto en bloques de ~'palabras' palabras, cortando en fin de oración.

    Devuelve [(bloque, separador)] tal que "".join(b + s) == texto: así los
    saltos de línea entre tramos se conservan al volver a unir.
    """
    piezas = re.split(r"(?<=[.!?…])(\s+)", texto)
    oraciones = [(piezas[i], piezas[i + 1] if i + 1 < len(piezas) else "") for i in range(0, len(piezas), 2)]

    # Una "oración" gigante (Whisper a veces no pone puntos) se corta por palabras.
    finas = []
    for oracion, sep in oraciones:
        tokens = re.split(r"(\s+)", oracion)
        if len(tokens) // 2 + 1 <= palabras:
            finas.append((oracion, sep))
            continue
        paso = palabras * 2
        trozos = ["".join(tokens[i:i + paso]) for i in range(0, len(tokens), paso)]
        for k, trozo in enumerate(trozos):
            # El espacio final de cada trozo pasa a ser su separador.
            limpio = trozo.rstrip()
            finas.append((limpio, trozo[len(limpio):] if k + 1 < len(trozos) else sep))

    bloques, actual, n_actual = [], "", 0
    for oracion, sep in finas:
        n = len(oracion.split())
        if actual and n_actual + n > palabras:
            limpio = actual.rstrip()
            bloques.append((limpio, actual[len(limpio):]))
            actual, n_actual = "", 0
        actual += oracion + sep
        n_actual += n
    if actual:
        limpio = actual.rstrip()
        bloques.append((limpio, actual[len(limpio):]))
    return bloques


def buscar_glosario(drive, carpeta_materia):
    """Texto del archivo 'Glosario*' en <materia>/Contexto (Google Doc o texto), o None."""
    contexto = next(
        (i for i in listar(drive, carpeta_materia)
         if i["mimeType"] == "application/vnd.google-apps.folder" and normalizar(i["name"]) == normalizar(NOMBRE_CONTEXTO)),
        None,
    )
    if not contexto:
        return None
    for item in listar(drive, contexto["id"]):
        es_texto = item["mimeType"] == "application/vnd.google-apps.document" or item["mimeType"].startswith("text/")
        if es_texto and normalizar(item["name"]).startswith("glosario"):
            return leer_texto(drive, item).strip() or None
    return None


def tema_de_clase(glosario, base):
    """Línea del cronograma del glosario que corresponde a la clase 'dd-mm' (o None).

    La fecha se busca solo antes del primer ':' de cada línea, porque el tema
    puede mencionar otras fechas ("intercambiada con la del 14-09").
    """
    m = re.fullmatch(r"\s*(\d{1,2})[-/.](\d{1,2})\b.*", base or "")
    if not glosario or not m:
        return None
    dia, mes = int(m.group(1)), int(m.group(2))
    patron = re.compile(rf"(?<!\d)0?{dia}[-/.]0?{mes}(?!\d)")
    for linea in glosario.splitlines():
        if ":" in linea and patron.search(linea.split(":", 1)[0]):
            return linea.strip()
    return None


def palabras_del_glosario(texto):
    """Palabras significativas (sin tildes, en minúscula, 3+ letras, sin vacías) de un texto."""
    return [p for p in re.findall(r"[^\W_]+", forma_comparable(texto))
            if len(p) >= 3 and p not in PALABRAS_VACIAS_GLOSARIO]


def bigramas(palabra):
    return {palabra[i:i + 2] for i in range(len(palabra) - 1)}


def preparar_glosario_bloques(glosario):
    """Parte el glosario en lo fijo y las entradas que se eligen bloque por bloque.

    Fijo: lo que está antes del primer "== ... ==" (título, uso) y las secciones
    de docentes y cronograma. Entradas: cada línea de las demás secciones
    (autores, instituciones, conceptos...). Un glosario sin secciones "== ==" se
    manda entero, como antes.
    """
    fijo, entradas, encabezado, es_fija = [], [], None, True
    for linea in (glosario or "").splitlines():
        if not linea.strip():
            continue
        if linea.strip().startswith("=="):
            encabezado = linea.strip()
            es_fija = any(k in forma_comparable(linea) for k in SECCIONES_FIJAS_GLOSARIO)
            if es_fija:
                fijo.append("\n" + linea.strip())
            continue
        if es_fija:
            fijo.append(linea)
        else:
            palabras = list(dict.fromkeys(palabras_del_glosario(linea)))
            entradas.append({"encabezado": encabezado, "linea": linea, "palabras": palabras})
    frecuencia = {}   # en cuántas entradas aparece cada palabra (para dar más peso a las raras)
    for e in entradas:
        for palabra in e["palabras"]:
            frecuencia[palabra] = frecuencia.get(palabra, 0) + 1
    return {
        "fijo": "\n".join(fijo).strip(),
        "entradas": entradas,
        "frecuencia": frecuencia,
        "caracteres_entradas": sum(len(e["linea"]) + 1 for e in entradas),
        "caracteres_totales": len(glosario or ""),
    }


def seleccionar_glosario(prep, bloque):
    """Texto del glosario para un bloque: lo fijo + las entradas que aparecen o se parecen a algo del bloque."""
    if not prep["entradas"]:
        return prep["fijo"]
    palabras_bloque = set(palabras_del_glosario(bloque))
    # Plural: "RCTs" en el bloque también alcanza a "RCT" del glosario.
    exactas = palabras_bloque | {p[:-1] for p in palabras_bloque if p.endswith("s") and len(p) > 3}
    largas = {p: bigramas(p) for p in palabras_bloque if len(p) >= 4}

    mejor_por_palabra = {}   # palabra del glosario -> mejor similitud con alguna palabra del bloque

    def relevancia(t):
        if t in exactas:
            return 1.0
        if t in mejor_por_palabra:
            return mejor_por_palabra[t]
        mejor = 0.0
        if len(t) >= 5:      # las palabras cortas solo cuentan si aparecen tal cual
            bt = bigramas(t)
            for w, bw in largas.items():
                if abs(len(w) - len(t)) > 0.4 * max(len(w), len(t)):
                    continue
                if 2 * len(bt & bw) / (len(bt) + len(bw)) < 0.3:
                    continue
                mejor = max(mejor, similitud_normalizada(t, w))
        mejor_por_palabra[t] = mejor
        return mejor

    # Puntaje de una entrada: suma de (parecido x rareza) de sus palabras que coinciden. Una
    # palabra que está en muchas entradas ("modelo", "autorregresivo") pesa poco; una que casi
    # no se repite (un apellido, una variante) pesa mucho. Si el tope de caracteres no alcanza
    # para todas, quedan las más específicas.
    total = len(prep["entradas"])
    puntuadas = []
    for orden, e in enumerate(prep["entradas"]):
        puntaje = sum(v * math.log(1 + total / prep["frecuencia"][t])
                      for t, v in ((t, relevancia(t)) for t in e["palabras"]) if v >= UMBRAL_RELEVANCIA_GLOSARIO)
        if puntaje > 0:
            puntuadas.append((puntaje, -orden, orden, e))
    puntuadas.sort(key=lambda x: x[:3], reverse=True)

    elegidas, usados = [], 0
    for _, _, orden, e in puntuadas:
        if usados + len(e["linea"]) + 1 > MAX_CARACTERES_GLOSARIO_VARIABLE:
            continue
        elegidas.append((orden, e))
        usados += len(e["linea"]) + 1
    elegidas.sort(key=lambda x: x[0])

    partes, ultimo = [prep["fijo"]], None
    for _, e in elegidas:
        if e["encabezado"] != ultimo:
            partes.append("\n" + e["encabezado"])
            ultimo = e["encabezado"]
        partes.append(e["linea"])
    return "\n".join(partes).strip()


def palabras_por_bloque(glosario):
    """Achica los bloques si el glosario es largo, para entrar holgado en el límite por minuto.

    Acepta el texto del glosario o su versión preparada (preparar_glosario_bloques).
    Cuenta lo fijo más, como máximo, las entradas que se mandan por bloque.
    """
    prep = glosario if isinstance(glosario, dict) else preparar_glosario_bloques(glosario)
    caracteres = len(prep["fijo"]) + min(prep["caracteres_entradas"], MAX_CARACTERES_GLOSARIO_VARIABLE)
    # prompt + glosario (~3,5 caracteres por token) + razonamiento del modelo
    tokens_fijos = (len(PROMPT_CORRECCION) + caracteres) / 3.5 + 1000
    disponible = LIMITE_TOKENS_MINUTO_CORRECCION * 0.85 - tokens_fijos
    # Entrada + salida (texto corregido + lista de correcciones) ≈ 2,1 veces el bloque.
    return max(200, min(PALABRAS_POR_BLOQUE, int(disponible / (TOKENS_POR_PALABRA * 2.1))))


def esperar_limite_minuto(tokens_estimados):
    """Si el último pedido dejó pocos tokens por minuto, espera a que se renueven."""
    restantes, renueva_en = ESTADO.get("correccion_restantes"), ESTADO.get("correccion_renueva")
    if restantes is None or renueva_en is None or restantes >= tokens_estimados:
        return
    espera = renueva_en - time.time()
    if espera > 0:
        log(f"Corrección: esperando {espera:.0f}s por el límite de tokens por minuto.")
        time.sleep(espera + 1)


def anotar_limites(r):
    restantes = r.headers.get("x-ratelimit-remaining-tokens")
    renueva = segundos_de_duracion(r.headers.get("x-ratelimit-reset-tokens"))
    try:
        ESTADO["correccion_restantes"] = int(float(restantes)) if restantes is not None else None
    except ValueError:
        ESTADO["correccion_restantes"] = None
    ESTADO["correccion_renueva"] = time.time() + renueva if renueva is not None else None


def normalizar_correcciones(lista):
    """Acepta [{'original','corregido'}], [['a','b']] o ['a → b'] y devuelve [(a, b)]."""
    pares = []
    for c in lista if isinstance(lista, list) else []:
        a = b = None
        if isinstance(c, dict):
            a = c.get("original", c.get("de"))
            b = c.get("corregido", c.get("a", c.get("correccion")))
        elif isinstance(c, (list, tuple)) and len(c) == 2:
            a, b = c
        elif isinstance(c, str):
            partes = re.split(r"\s*(?:→|->|=>)\s*", c, maxsplit=1)
            if len(partes) == 2:
                a, b = partes
        if isinstance(a, str) and isinstance(b, str) and a.strip() and b.strip() and a.strip() != b.strip():
            pares.append((a.strip(), b.strip()))
    return list(dict.fromkeys(pares))


def corregir_bloque(groq_key, bloque, glosario, tema, contexto):
    """Pide la corrección de un bloque. Devuelve (texto, [(original, corregido)]).

    'glosario' es el texto que se le muestra al modelo para este bloque
    (ver seleccionar_glosario); None si la materia no tiene.
    """
    usuario = (
        f"GLOSARIO DE LA MATERIA{' (docentes, cronograma y solo las entradas relacionadas con este bloque)' if glosario else ''}:\n"
        f"{glosario or '(no hay glosario para esta materia)'}\n\n"
        f"CLASE: {contexto['clase']} | TEMA SEGÚN CRONOGRAMA: {tema or 'no figura'}\n\n"
        f"BLOQUE {contexto['n']} DE {contexto['total']} A CORREGIR:\n<<<\n{bloque}\n>>>"
    )
    tokens_bloque = len(bloque.split()) * TOKENS_POR_PALABRA
    max_salida = int(tokens_bloque * 1.3) + 1500
    esperar_limite_minuto(int((len(PROMPT_CORRECCION) + len(usuario)) / 3.5) + max_salida)

    def _llamar():
        try:
            r = requests.post(
                GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {groq_key}"},
                json={
                    "model": MODELO_CORRECCION,
                    "temperature": 0,
                    "reasoning_effort": "low",
                    "max_completion_tokens": max_salida,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": PROMPT_CORRECCION},
                        {"role": "user", "content": usuario},
                    ],
                },
                timeout=TIMEOUT_GROQ_S,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ErrorReintentable(f"sin respuesta ({type(e).__name__})") from e
        anotar_limites(r)
        if r.status_code == 200:
            return r.json()
        try:
            error = r.json().get("error", {})
        except ValueError:
            error = {}
        mensaje = error.get("message", r.text[:300])
        if r.status_code == 429:
            espera = segundos_de_duracion(r.headers.get("retry-after")) or segundos_espera_groq(r)
            if re.search(r"per day|\((TPD|RPD)\)", mensaje, re.I):
                raise SinCuotaCorreccion("cuota diaria del corrector agotada.")
            if espera is not None and espera > MAX_ESPERA_GROQ_S:
                raise SinCuotaCorreccion(f"el corrector pide esperar {espera / 60:.0f} min.")
            raise ErrorReintentable("respondió 429 (límite por minuto)", espera=espera)
        if r.status_code >= 500:
            raise ErrorReintentable(f"respondió {r.status_code}")
        if r.status_code == 400 and error.get("code") == "json_validate_failed":
            raise BloqueDescartado("el modelo no devolvió JSON válido")
        raise RuntimeError(f"respondió {r.status_code}: {mensaje}")

    respuesta = con_reintentos(f"Corrección ({contexto['clase']}, bloque {contexto['n']}/{contexto['total']})", _llamar)
    uso = respuesta.get("usage") if isinstance(respuesta, dict) else None
    if isinstance(uso, dict):
        USO_CORRECCION["prompt"] += int(uso.get("prompt_tokens") or 0)
        USO_CORRECCION["completion"] += int(uso.get("completion_tokens") or 0)
        USO_CORRECCION["pedidos"] += 1
    try:
        contenido = respuesta["choices"][0]["message"]["content"]
        datos = json.loads(contenido)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise BloqueDescartado(f"JSON ilegible ({type(e).__name__})") from e
    propuestas = normalizar_correcciones(datos.get("correcciones")) if isinstance(datos, dict) else []
    texto = datos.get("texto") if isinstance(datos, dict) else None
    if not isinstance(texto, str) or not texto.strip():
        raise BloqueDescartado("el JSON no trae 'texto'", propuestas)
    texto = texto.strip()
    proporcion = len(texto) / max(1, len(bloque))
    if not MIN_PROPORCION_CORRECCION <= proporcion <= MAX_PROPORCION_CORRECCION:
        raise BloqueDescartado(f"largo {proporcion:.0%} del original", propuestas)
    return texto, propuestas


def normalizar_guiones(texto):
    return texto.translate(TABLA_GUIONES)


def forma_comparable(texto):
    """Minúsculas, sin tildes y con guiones comunes (para comparar con el glosario)."""
    return normalizar_guiones(normalizar(texto))


def similitud(a, b):
    """1 - distancia de Levenshtein / largo mayor, sobre las formas comparables."""
    return similitud_normalizada(forma_comparable(a), forma_comparable(b))


def similitud_normalizada(a, b):
    """Igual que similitud() pero sobre textos que ya pasaron por forma_comparable."""
    if not a and not b:
        return 1.0
    previa = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        actual = [i]
        for j, cb in enumerate(b, 1):
            actual.append(min(previa[j] + 1, actual[j - 1] + 1, previa[j - 1] + (ca != cb)))
        previa = actual
    return 1 - previa[-1] / max(len(a), len(b))


def preparar_glosario(glosario):
    """Datos del glosario para el filtro: texto comparable, apodos, variantes y conceptos.

    Apodos = formas válidas de trato, escritas en el glosario entre paréntesis
    con una aclaración después de ':' (ej. "Luciana Petrone (Luz, Lu: así la
    nombran en clase)"). Los paréntesis sin ':' son variantes erróneas. Los
    títulos de sección ("== CRONOGRAMA (fecha: tema: docente) ==") no cuentan.
    """
    apodos, variantes, conceptos, seccion = set(), {}, [], ""
    for linea in (glosario or "").splitlines():
        if linea.strip().startswith("=="):
            seccion = forma_comparable(linea)
            continue
        if "concepto" in seccion or "termino" in seccion:
            # Sustantivos comunes: van en minúscula a mitad de frase.
            conceptos.append(forma_comparable(re.sub(r"\([^()]*\)", "", linea)))
        for contenido in re.findall(r"\(([^()]*)\)", linea):
            if ":" in contenido:
                for forma in re.split(r"[,/]", contenido.split(":", 1)[0]):
                    if forma.strip():
                        apodos.add(forma_comparable(forma.strip()))
            else:
                # Variante errónea -> línea del término correcto (ej. "Kabir" -> "Naila Kabeer (...)")
                for forma in re.split(r"[,/]", contenido):
                    if forma.strip():
                        variantes.setdefault(forma_comparable(forma.strip()), []).append(forma_comparable(linea))
    return {"texto": forma_comparable(glosario or ""), "apodos": apodos, "variantes": variantes,
            "conceptos": conceptos}


def sin_plural_sigla(termino):
    """'RCTs' -> 'RCT' (siglas en plural); el resto queda igual."""
    t = normalizar_guiones(termino.strip())
    return t[:-1] if re.fullmatch(r"[A-Z0-9-]{2,}s", t) else t


def contiene_termino(texto, termino):
    patron = rf"(?<!\w){re.escape(forma_comparable(termino))}(?!\w)"
    return bool(texto) and re.search(patron, texto) is not None


def esta_en_glosario(termino, info):
    return contiene_termino(info["texto"], termino) or contiene_termino(info["texto"], sin_plural_sigla(termino))


def similitud_nombre(original, corregido):
    """Similitud para nombres: con la misma cantidad de palabras, la PEOR palabra a palabra.

    Así "Mohamed Shams → Muhammad Yunus" da 0,20 (Shams/Yunus) y no 0,50: un
    apellido distinto no pasa por tener el mismo nombre de pila.
    """
    a, b = sin_plural_sigla(original).split(), sin_plural_sigla(corregido).split()
    if len(a) == len(b) > 1:
        return min(similitud(x, y) for x, y in zip(a, b))
    return similitud(" ".join(a), " ".join(b))


def es_variante(original, corregido, info):
    """True si el glosario lista 'original' como variante errónea del término 'corregido'."""
    lineas = info["variantes"].get(forma_comparable(original), []) + \
        info["variantes"].get(forma_comparable(sin_plural_sigla(original)), [])
    return any(contiene_termino(l, corregido) or contiene_termino(l, sin_plural_sigla(corregido)) for l in lineas)


def patron_palabra(frase):
    return re.compile(rf"(?<!\w){re.escape(frase)}(?!\w)")


def es_nombre_propio(original, corregido, bloque):
    """True si la corrección es un nombre propio o sigla; None si no se puede saber.

    Una mayúscula al principio de oración no alcanza para decir que es nombre
    propio: en ese caso devuelve None y decide el glosario.
    """
    palabras = re.findall(r"[^\W\d_][\w'-]*", corregido)
    if not palabras:
        return False
    if any(len(p) >= 2 and p.isupper() for p in palabras):
        return True   # sigla
    if any(p[0].isupper() for p in palabras[1:]):
        return True
    if not palabras[0][0].isupper():
        return False
    if not original[:1].isupper():
        return True   # el modelo le puso mayúscula: lo trata como nombre
    for m in patron_palabra(original).finditer(bloque):
        antes = bloque[:m.start()].rstrip()
        if antes and antes[-1] not in ".!?…¿¡:\"«":
            return True   # aparece con mayúscula en medio de una oración
    return None


def completa_fragmento(palabras_original, palabras_corregidas):
    """True si alguna palabra del original es el COMIENZO de la palabra corregida (un fragmento completado).

    Se evalúa palabra por palabra ("pa est" -> "país está": pa -> país y est -> está). Para aceptar
    una completación habría que garantizar que la palabra elegida concuerda en género y número con
    las vecinas ("los pa" pide "países", no "país"), y eso no se puede garantizar sin un análisis
    gramatical: ante la duda se descarta. Es preferible un falso descarte a una corrección mala.
    """
    def limpia(palabra):
        return re.sub(r"^\W+|\W+$", "", forma_comparable(palabra))

    for w, c in zip(palabras_original, palabras_corregidas):
        fw, fc = limpia(w), limpia(c)
        if fw and len(fc) > len(fw) and fc.startswith(fw):
            return True
    return False


def filtrar_correccion(original, corregido, bloque, info):
    """Devuelve None si la corrección se acepta, o el nombre de la regla que la descarta."""
    def solo_letras(t):
        return "".join(c for c in normalizar_guiones(t).casefold() if c.isalnum())

    if solo_letras(original) == solo_letras(corregido):
        return "solo formato o puntuación"
    if sorted(re.findall(r"\d", original)) != sorted(re.findall(r"\d", corregido)) or \
            (re.search(r"\d", original) and solo_letras(re.sub(r"\d", "", original)) == solo_letras(re.sub(r"\d", "", corregido))):
        return "cambia números"
    palabras_o, palabras_c = original.split(), corregido.split()
    if len(palabras_c) > len(palabras_o):
        return "agrega palabras"
    if len(palabras_c) < len(palabras_o):
        # "Estados Unidos China" -> "Estados Unidos" perdía una palabra de la clase.
        return "quita palabras"
    if forma_comparable(original) in info["apodos"]:
        return "apodo o forma de trato del glosario"
    a, b = forma_comparable(original), forma_comparable(corregido)
    if len(b) > len(a) and b.startswith(a):
        return "completa una palabra cortada"
    if completa_fragmento(palabras_o, palabras_c):
        return "completa una palabra cortada"
    if not patron_palabra(original).search(bloque):
        return "no aparece en el bloque"
    if (forma_comparable(original) in info["variantes"] or forma_comparable(sin_plural_sigla(original)) in info["variantes"]) \
            and not es_variante(original, corregido, info):
        return "variante de otro término del glosario"

    propio = es_nombre_propio(original, corregido, bloque)
    en_glosario = esta_en_glosario(corregido, info)
    if propio or (propio is None and en_glosario):
        if not en_glosario:
            return "nombre propio o sigla fuera del glosario"
        if es_variante(original, corregido, info):
            return None
        valor = similitud_nombre(original, corregido)
        if valor < UMBRAL_SIMILITUD_NOMBRES:
            return f"nombre propio poco parecido ({valor:.2f} < {UMBRAL_SIMILITUD_NOMBRES})"
        return None
    valor = similitud(original, corregido)
    if valor < UMBRAL_SIMILITUD:
        return f"similitud baja ({valor:.2f} < {UMBRAL_SIMILITUD})"
    return None


def es_concepto(termino, info):
    """True si el término es un sustantivo común del glosario (sección CONCEPTOS Y TÉRMINOS)."""
    return any(contiene_termino(c, termino) for c in info.get("conceptos", []))


def ajustar_concepto(corregido, info):
    """'Econometría' -> 'econometría' si es un concepto del glosario escrito solo con mayúscula inicial.

    Devuelve (texto, ajustable): los ajustables se escriben con mayúscula solo
    a principio de oración (ver aplicar_correcciones).
    """
    palabras = corregido.split()
    if not corregido[:1].isupper() or any(len(p) >= 2 and p.isupper() for p in palabras) or \
            any(p[:1].isupper() for p in palabras[1:]) or not es_concepto(corregido, info):
        return corregido, False
    return corregido[0].lower() + corregido[1:], True


def inicio_de_oracion(texto, posicion):
    antes = texto[:posicion].rstrip()
    return not antes or antes[-1] in ".!?…¿¡"


def aplicar_correcciones(bloque, pares, ajustables=()):
    """Aplica las correcciones aceptadas sobre el bloque ORIGINAL, en una sola pasada.

    Las correcciones 'ajustables' (conceptos) van en minúscula a mitad de frase
    y con mayúscula inicial a principio de oración.
    """
    if not pares:
        return bloque
    reemplazos = dict(pares)
    for original, nuevo in pares:
        # Un concepto también se corrige cuando aparece con mayúscula por inicio de oración.
        if nuevo in ajustables and original[:1].islower():
            reemplazos.setdefault(original[0].upper() + original[1:], nuevo)
    patron = re.compile("|".join(
        rf"(?<!\w){re.escape(a)}(?!\w)" for a in sorted(reemplazos, key=len, reverse=True)))

    def reemplazar(m):
        nuevo = reemplazos[m.group(0)]
        if nuevo in ajustables and inicio_de_oracion(bloque, m.start()):
            nuevo = nuevo[0].upper() + nuevo[1:]
        return nuevo

    return patron.sub(reemplazar, bloque)


def corregir_transcripcion(groq_key, texto, glosario, clase, etiqueta):
    """Corrige la transcripción por bloques. Nunca falla: ante problemas usa el original.

    Devuelve (texto final, correcciones sin repetir, resumen dict). En
    resumen["completa"] va True solo si se procesaron TODOS los bloques (los
    descartados por el modelo cuentan como procesados): quien reemplaza un Doc
    tiene que usar el resultado solo si la corrección quedó completa.
    """
    resumen = {"bloques": 0, "corregidos": 0, "descartados": 0, "sin_corregir": 0, "auditoria": [],
               "completa": False}
    if not groq_key or ESTADO["sin_cuota_correccion"]:
        motivo = "sin GROQ_API_KEY" if not groq_key else "cuota del corrector agotada en esta corrida"
        log(f"{etiqueta}: se guarda sin corregir ({motivo}).")
        return texto, [], dict(resumen, sin_corregir=1, motivo=motivo)

    tema = tema_de_clase(glosario, clase)
    info = preparar_glosario(glosario)
    prep = preparar_glosario_bloques(glosario)
    palabras = palabras_por_bloque(prep)
    bloques = dividir_en_bloques(texto, palabras)
    resumen["bloques"] = len(bloques)
    log(f"{etiqueta}: corrigiendo {len(bloques)} bloque(s) de ~{palabras} palabras con {MODELO_CORRECCION} "
        f"({'con' if glosario else 'sin'} glosario; tema: {tema or 'no figura'}).")
    if glosario:
        log(f"{etiqueta}: glosario completo ≈ {prep['caracteres_totales'] / 3.5:.0f} tokens; por bloque se manda lo fijo "
            f"(≈ {len(prep['fijo']) / 3.5:.0f}) más las entradas relacionadas "
            f"({len(prep['entradas'])} entradas en total).")
    enviados = []   # caracteres de glosario mandados en cada bloque
    uso_antes = dict(USO_CORRECCION)

    salida, correcciones, fallas_seguidas, cortar = [], [], 0, None
    for n, (bloque, sep) in enumerate(bloques, 1):
        if cortar is None and ESTADO["sin_cuota_correccion"]:
            cortar = "cuota del corrector agotada"
        if cortar is None and tiempo_agotado():
            cortar = "se acabó el tiempo de la corrida"
        if cortar:
            salida.append(bloque + sep)
            resumen["sin_corregir"] += 1
            continue
        try:
            glosario_bloque = seleccionar_glosario(prep, bloque) if glosario else None
            enviados.append(len(glosario_bloque or ""))
            _, propuestas = corregir_bloque(groq_key, bloque, glosario_bloque, tema,
                                            {"clase": clase, "n": n, "total": len(bloques)})
            aceptadas, ajustables = [], set()
            for original, corregido in propuestas:
                corregido = normalizar_guiones(corregido)
                if any(original == a for a, _ in aceptadas):
                    regla = "repetida en el bloque"
                else:
                    regla = filtrar_correccion(original, corregido, bloque, info)
                resumen["auditoria"].append({"bloque": n, "original": original, "correccion": corregido,
                                             "estado": "aceptada" if regla is None else "descartada",
                                             "regla": regla or ""})
                if regla is None:
                    corregido, ajustable = ajustar_concepto(corregido, info)
                    if ajustable:
                        ajustables.add(corregido)
                        resumen["auditoria"][-1]["correccion"] = corregido
                    aceptadas.append((original, corregido))
            salida.append(aplicar_correcciones(bloque, aceptadas, ajustables) + sep)
            correcciones += aceptadas
            resumen["corregidos"] += 1
            fallas_seguidas = 0
            log(f"{etiqueta}: bloque {n}/{len(bloques)}: {len(aceptadas)} de {len(propuestas)} corrección(es) aceptada(s).")
        except BloqueDescartado as e:
            salida.append(bloque + sep)
            resumen["descartados"] += 1
            for original, corregido in e.propuestas:
                resumen["auditoria"].append({"bloque": n, "original": original, "correccion": corregido,
                                             "estado": "descartada", "regla": f"bloque descartado: {e}"})
            log(f"{etiqueta}: bloque {n}/{len(bloques)} DESCARTADO ({e}); queda el original.")
        except SinCuotaCorreccion as e:
            ESTADO["sin_cuota_correccion"] = True
            salida.append(bloque + sep)
            resumen["sin_corregir"] += 1
            log(f"{etiqueta}: {e} El resto queda sin corregir.")
        except Exception as e:  # noqa: BLE001 - la corrección nunca frena la transcripción
            salida.append(bloque + sep)
            resumen["sin_corregir"] += 1
            fallas_seguidas += 1
            log(f"{etiqueta}: bloque {n}/{len(bloques)} sin corregir: {e}")
            if fallas_seguidas >= 2:
                cortar = "el corrector falló dos bloques seguidos"
    if cortar:
        log(f"{etiqueta}: corrección cortada ({cortar}); lo que faltaba queda sin corregir.")
        resumen["motivo"] = cortar

    resumen["completa"] = resumen["sin_corregir"] == 0 and not cortar
    unicas = list(dict.fromkeys(correcciones))
    auditoria = resumen["auditoria"]
    resumen["propuestas"] = len(auditoria)
    pedidos = USO_CORRECCION["pedidos"] - uso_antes["pedidos"]
    if pedidos:
        entrada = USO_CORRECCION["prompt"] - uso_antes["prompt"]
        salida_tok = USO_CORRECCION["completion"] - uso_antes["completion"]
        log(f"{etiqueta}: tokens reales de Groq en esta clase: entrada {entrada} + salida {salida_tok} = {entrada + salida_tok} "
            f"en {pedidos} pedido(s) (≈ {(entrada + salida_tok) / pedidos:.0f} por bloque).")
    if enviados:
        log(f"{etiqueta}: glosario mandado por bloque: ≈ {sum(enviados) / len(enviados) / 3.5:.0f} tokens en promedio "
            f"(máximo ≈ {max(enviados) / 3.5:.0f}; completo ≈ {prep['caracteres_totales'] / 3.5:.0f}).")
    resumen["filtradas"] = sum(1 for f in auditoria if f["estado"] == "descartada")
    log(f"{etiqueta}: corrección lista: {resumen['corregidos']} bloque(s) corregido(s), "
        f"{resumen['descartados']} descartado(s), {resumen['sin_corregir']} sin corregir; "
        f"{resumen['propuestas']} corrección(es) propuesta(s), {resumen['filtradas']} descartada(s) por el filtro, "
        f"{len(unicas)} aplicada(s) distinta(s).")
    return "".join(salida), unicas, resumen


def resumen_auditoria(auditoria):
    """Líneas de resumen: aceptadas y descartadas por regla."""
    reglas = {}
    for f in auditoria:
        if f["estado"] == "descartada":
            clave = re.sub(r" \(.*\)$", "", f["regla"])
            reglas[clave] = reglas.get(clave, 0) + 1
    aceptadas = sum(1 for f in auditoria if f["estado"] == "aceptada")
    lineas = [f"Auditoría: {len(auditoria)} propuesta(s), {aceptadas} aceptada(s), {len(auditoria) - aceptadas} descartada(s)."]
    lineas += [f"  descartadas por '{r}': {c}" for r, c in sorted(reglas.items(), key=lambda x: -x[1])]
    return lineas


def auditoria_csv(auditoria):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["bloque", "original", "correccion", "estado", "regla"], lineterminator="\n")
    w.writeheader()
    w.writerows(auditoria)
    return buf.getvalue()


def seccion_correcciones(correcciones, resumen):
    lineas = ["", "", SEPARADOR_CORRECCIONES, TITULO_CORRECCIONES,
              f"(automáticas con {MODELO_CORRECCION}; bloques: {resumen['bloques']}, corregidos: {resumen['corregidos']}, "
              f"descartados: {resumen['descartados']}, sin corregir: {resumen['sin_corregir']}"
              + (f"; correcciones propuestas: {resumen['propuestas']}, descartadas por el filtro: {resumen['filtradas']}"
                 if resumen.get("propuestas") else "")
              + (f" — {resumen['motivo']}" if resumen.get("motivo") else "") + ")"]
    lineas += [f"- {a} → {b}" for a, b in correcciones] or ["- ninguna"]
    return "\n".join(lineas)


# La marca es el título seguido de la línea "(automáticas con ...". La línea de separador de
# arriba es opcional y puede ser cualquier fila de símbolos: no se depende de que Docs la conserve.
# Tolera saltos de línea de más (Docs podría exportar líneas en blanco entre párrafos).
PATRON_SECCION = re.compile(rf"(?:\s*[^\w\s]{{3,}})?\s*{re.escape(TITULO_CORRECCIONES)}\s+\(autom")


def quitar_seccion_correcciones(texto):
    """Saca la sección 'Correcciones aplicadas' de un Doc ya corregido (para re-corregir)."""
    m = PATRON_SECCION.search(texto)
    return texto[:m.start()].rstrip() if m else texto


def estado_correccion(texto):
    """'sin' (no tiene la sección), 'parcial' (la tiene pero quedaron bloques sin corregir) o 'completa'.

    La sección "Correcciones aplicadas" es la marca. En los Docs viejos que se
    guardaron cuando se acabó la cuota dice "sin corregir: N" con N > 0.
    """
    m = PATRON_SECCION.search(texto)
    if not m:
        return "sin"
    faltan = re.search(r"sin corregir: (\d+)", texto[m.end():m.end() + 800])
    return "parcial" if faltan and int(faltan.group(1)) > 0 else "completa"


def normalizar(nombre):
    """Para comparar nombres ignorando tildes, mayúsculas y espacios de más."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", nombre) if not unicodedata.combining(c)
    )
    return " ".join(sin_tildes.casefold().split())


# ---------------------------------------------------------------------------
# Procesamiento
# ---------------------------------------------------------------------------

def glosario_de(drive, carpeta_materia, materia):
    """Glosario de la materia (se lee una vez por corrida)."""
    if carpeta_materia not in GLOSARIOS:
        try:
            GLOSARIOS[carpeta_materia] = buscar_glosario(drive, carpeta_materia)
        except Exception as e:  # noqa: BLE001 - sin glosario se corrige igual
            log(f"[{materia}] no pude leer el glosario ({e}); corrijo sin glosario.")
            GLOSARIOS[carpeta_materia] = None
        if GLOSARIOS[carpeta_materia] is None:
            log(f"[{materia}] sin glosario en '{NOMBRE_CONTEXTO}'.")
    return GLOSARIOS[carpeta_materia]


def transcribir_audio(drive, claves, materia, audio, carpeta_trans, carpeta_partes, salida=None, solo_groq=False,
                      carpeta_materia=None):
    """Transcribe un audio por tramos y guarda el Doc 'salida' (por defecto, el nombre del audio).

    Con carpeta_materia, después de unir los tramos corrige el texto
    (glosario + Groq chat) y guarda la versión cruda en _partes.
    """
    base = nombre_base(audio["name"])
    salida = salida or base
    etiqueta = f"[{materia}] {salida}"

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "audio_original"
        log(f"{etiqueta}: bajando audio ({int(audio.get('size', 0)) // (1024 * 1024)} MB)...")
        bajar(drive, audio["id"], ruta)

        duracion = duracion_segundos(ruta)
        total = max(1, math.ceil(duracion / SEGUNDOS_POR_TRAMO))
        largo = math.ceil(duracion / total) + 1  # +1 s para no perder el final
        log(f"{etiqueta}: {duracion / 60:.0f} min -> {total} tramo(s) de ~{largo / 60:.0f} min.")

        partes_existentes = archivos_por_nombre(drive, carpeta_partes)
        nombres_partes = [f"{salida} - parte {i + 1} de {total}" for i in range(total)]

        for i, nombre_parte in enumerate(nombres_partes):
            if nombre_parte in partes_existentes:
                log(f"{etiqueta}: tramo {i + 1}/{total} ya estaba hecho.")
                continue
            if tiempo_agotado():
                log(f"{etiqueta}: se acabó el tiempo de esta corrida; sigue en la próxima.")
                return False

            tramo = Path(tmp) / f"tramo_{i + 1}.mp3"
            extraer_tramo(ruta, i * (largo - 1), largo, tramo)
            texto, motor = transcribir_tramo(claves, tramo, f"{salida}, tramo {i + 1}/{total}", solo_groq)

            guardar_texto(drive, carpeta_partes, nombre_parte, texto)
            partes_existentes[nombre_parte] = True
            log(f"{etiqueta}: tramo {i + 1}/{total} OK con {motor} ({len(texto)} caracteres).")
            if motor == "gemini" and i + 1 < total:
                # Cada tramo son ~77.000 tokens: pausa para no pasar el límite por minuto de Gemini.
                time.sleep(PAUSA_ENTRE_TRAMOS_S)

    # Todos los tramos listos: unir, corregir, guardar y limpiar
    partes = archivos_por_nombre(drive, carpeta_partes)
    textos = [leer_texto(drive, partes[n]).strip() for n in nombres_partes]
    texto = "\n\n".join(textos)
    if carpeta_materia:
        if salida + SUFIJO_SIN_CORREGIR not in partes:
            guardar_texto(drive, carpeta_partes, salida + SUFIJO_SIN_CORREGIR, texto)
        glosario = glosario_de(drive, carpeta_materia, materia)
        corregido, correcciones, resumen = corregir_transcripcion(claves.get("groq"), texto, glosario, base, etiqueta)
        for linea in resumen_auditoria(resumen["auditoria"]) if resumen["auditoria"] else []:
            log(f"{etiqueta}: {linea}")
        if resumen["completa"]:
            texto = corregido + seccion_correcciones(correcciones, resumen)
            CORREGIDOS_EN_ESTA_CORRIDA.add((materia, salida))
        else:
            # Nunca un Doc a medias: sin corregir y sin sección. La fase de pendientes
            # lo corrige entero, desde el original de _partes, cuando haya cuota.
            log(f"{etiqueta}: la corrección quedó incompleta ({resumen.get('motivo', 'ver arriba')}); "
                f"el Doc se guarda sin corregir y queda para la fase de pendientes.")
    guardar_texto(drive, carpeta_trans, salida, texto, como_doc=True)
    for n in nombres_partes:
        a_papelera(drive, partes[n]["id"])
    log(f"{etiqueta}: transcripción completa guardada.")
    return True


def resumir(drive, api_key, materia, base, transcripcion, carpeta_trans):
    etiqueta = f"[{materia}] {base}"
    texto = leer_texto(drive, transcripcion)
    prompt = PROMPT_RESUMEN.format(materia=materia, clase=base, transcripcion=texto)

    ultimo_error = None
    for modelo in MODELOS_RESUMEN:
        if tiempo_agotado():
            break
        try:
            resumen = generar(api_key, modelo, [{"text": prompt}], f"resumen de {base}")
            guardar_texto(drive, carpeta_trans, base + SUFIJO_RESUMEN, resumen, como_doc=True)
            log(f"{etiqueta}: resumen guardado (con {modelo}).")
            return
        except (ErrorReintentable, RuntimeError, CuotaDiariaAgotada) as e:
            # La cuota es por modelo: si uno la agotó, otro puede tener.
            ultimo_error = e
            log(f"{etiqueta}: {modelo} no pudo con el resumen ({e}); pruebo el siguiente.")
    log(f"{etiqueta}: resumen pendiente para la próxima corrida. Último error: {ultimo_error}")


def preparar_materia(drive, materia, carpeta_id):
    """Carpetas de salida y lista de audios de una materia."""
    carpeta_trans = subcarpeta(drive, carpeta_id, NOMBRE_TRANSCRIPCIONES)
    carpeta_partes = subcarpeta(drive, carpeta_trans, NOMBRE_PARTES)
    audios = []
    for item in listar(drive, carpeta_id):
        if item["mimeType"] in ("application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"):
            continue
        if es_audio(item):
            audios.append(item)
        else:
            log(f"[{materia}] salteo '{item['name']}' (tipo {item['mimeType']}, no parece audio).")
    return {"materia": materia, "carpeta": carpeta_id, "trans": carpeta_trans, "partes": carpeta_partes, "audios": audios}


def resumir_pendientes(drive, api_key, m):
    """Fase 1: resumir toda transcripción que todavía no tenga resumen."""
    existentes = archivos_por_nombre(drive, m["trans"])
    for audio in m["audios"]:
        if tiempo_agotado():
            return
        base = nombre_base(audio["name"])
        if base in existentes and base + SUFIJO_RESUMEN not in existentes:
            try:
                resumir(drive, api_key, m["materia"], base, existentes[base], m["trans"])
            except Exception as e:  # noqa: BLE001
                log(f"[{m['materia']}] {base}: ERROR al resumir: {e}")


def sin_transcriptor():
    """True si ni Groq ni Gemini pueden transcribir más en esta corrida."""
    return ESTADO["sin_cuota_gemini"] and ESTADO["sin_cuota_groq"]


def transcribir_pendientes(drive, claves, m):
    """Fase 2: transcribir audios sin transcripción (y resumirlos si hay tiempo).

    Los pendientes se procesan en orden aleatorio para que un audio
    problemático no se lleve siempre el primer intento (y la cuota) de
    cada corrida.
    """
    existentes = archivos_por_nombre(drive, m["trans"])
    pendientes = [a for a in m["audios"] if nombre_base(a["name"]) not in existentes]
    random.shuffle(pendientes)
    if pendientes:
        log(f"[{m['materia']}] {len(pendientes)} audio(s) sin transcribir: {', '.join(nombre_base(a['name']) for a in pendientes)}")

    for audio in pendientes:
        if tiempo_agotado() or sin_transcriptor():
            return
        base = nombre_base(audio["name"])
        try:
            if not transcribir_audio(drive, claves, m["materia"], audio, m["trans"], m["partes"],
                                     carpeta_materia=m["carpeta"]):
                return
        except CuotaDiariaAgotada as e:
            ESTADO["sin_cuota_gemini"] = True
            if sin_transcriptor():
                log(f"{e} Sin Groq ni Gemini: no se transcribe más hasta que se renueve la cuota.")
                return
            log(f"[{m['materia']}] {base}: {e} Sigo con el próximo audio.")
            continue
        except Exception as e:  # noqa: BLE001 - seguir con el resto aunque uno falle
            log(f"[{m['materia']}] {base}: ERROR al transcribir: {e}")
            continue

        # Recién transcripta: resumirla ya, si queda tiempo
        nuevos = archivos_por_nombre(drive, m["trans"])
        if HACER_RESUMENES and base in nuevos and not tiempo_agotado():
            try:
                resumir(drive, claves["gemini"], m["materia"], base, nuevos[base], m["trans"])
            except Exception as e:  # noqa: BLE001
                log(f"[{m['materia']}] {base}: ERROR al resumir: {e}")


def clave_fecha(nombre):
    """(mes, día) de un nombre 'dd-mm ...' para ordenar de la más vieja a la más nueva."""
    m = re.match(r"\s*(\d{1,2})[-/.](\d{1,2})", nombre or "")
    return (int(m.group(2)), int(m.group(1))) if m else (99, 99)


def minutos_restantes():
    return MAX_MINUTOS_CORRIDA - (time.time() - INICIO) / 60


def textos_equivalentes(esperado, leido):
    """True si lo leído de Drive es lo escrito (ignora saltos de línea y espacios que Docs normaliza)."""
    a, b = " ".join(esperado.split()), " ".join(leido.split())
    if a == b:
        return True
    return bool(a) and abs(len(a) - len(b)) <= 0.01 * len(a) and a[:300] == b[:300] and a[-300:] == b[-300:]


def reemplazar_doc(drive, doc, nuevo, previo, etiqueta):
    """Reemplaza el contenido de un Google Doc EN EL LUGAR: mismo archivo, mismo ID, mismo nombre.

    No lo borra ni lo recrea (se rompería el vínculo con el resumen de Spark y con
    la carpeta). Después de escribir lo vuelve a leer; si no coincide con lo
    escrito, restaura el contenido anterior y falla.
    """
    def escribir(texto):
        media = MediaIoBaseUpload(io.BytesIO(texto.encode("utf-8")), mimetype="text/plain", resumable=True)
        r = drive.files().update(
            fileId=doc["id"], media_body=media, fields="id, name, mimeType", supportsAllDrives=True,
        ).execute(num_retries=REINTENTOS_DRIVE)
        if r.get("id") != doc["id"] or r.get("name") != doc["name"] or r.get("mimeType") != MIME_DOC:
            raise RuntimeError(f"Drive devolvió otro archivo al actualizar: {r}")

    escribir(nuevo)
    try:
        leido = leer_texto(drive, {"id": doc["id"], "mimeType": MIME_DOC})
        coincide = textos_equivalentes(nuevo, leido)
    except Exception as e:  # noqa: BLE001
        coincide, leido = False, f"(no se pudo leer: {e})"
    if coincide:
        return
    log(f"{etiqueta}: lo que quedó en el Doc no coincide con lo escrito; restauro el contenido anterior.")
    escribir(previo)
    raise RuntimeError("el Doc no quedó como se escribió; se restauró el contenido anterior")


def listar_auditoria_en_log(etiqueta, auditoria):
    """Una línea por corrección propuesta: aceptada o descartada, y la regla que la descartó."""
    log(f"{etiqueta}: correcciones propuestas por el modelo ({len(auditoria)}):")
    for f in auditoria:
        estado = "OK        " if f["estado"] == "aceptada" else "DESCARTADA"
        log(f"    [{estado}] bloque {f['bloque']}: {f['original']} → {f['correccion']}"
            + (f"   ({f['regla']})" if f["regla"] else ""))


def informar_reemplazo(drive, m, doc, nombre_crudo, actual, original, corregido, antes, etiqueta):
    """Deja en el log la evidencia de que el Doc se reemplazó en el lugar y de que no quedó nada duplicado."""
    try:
        en_trans = listar(drive, m["trans"])
        mismos = [i for i in en_trans if i["name"] == doc["name"]]
        identico = len(mismos) == 1 and mismos[0]["id"] == doc["id"] and mismos[0]["name"] == doc["name"]
        ids_despues = ", ".join(i["id"] for i in mismos) or "(no aparece)"
        nombre_despues = repr(mismos[0]["name"]) if mismos else "-"
        tipo_despues = mismos[0]["mimeType"] if mismos else "-"
        veredicto = "IDÉNTICOS" if identico else "DISTINTOS"
        log(f"{etiqueta}: VERIFICACIÓN 1 (mismo archivo): antes id={doc['id']} nombre={doc['name']!r}; "
            f"después id={ids_despues} nombre={nombre_despues} tipo={tipo_despues} -> {veredicto}.")
        crudo = next((i for i in listar(drive, m["partes"]) if i["name"] == nombre_crudo), None)
        if crudo:
            n_crudo = len(leer_texto(drive, crudo).strip())
            log(f"{etiqueta}: VERIFICACIÓN 2 (original): en {NOMBRE_PARTES} está {nombre_crudo!r} (id={crudo['id']}, "
                f"{n_crudo} caracteres, {'igual' if n_crudo == len(original) else 'DISTINTO'} al usado para corregir).")
        else:
            log(f"{etiqueta}: VERIFICACIÓN 2 (original): FALTA {nombre_crudo!r} en {NOMBRE_PARTES}.")
        leido = leer_texto(drive, doc).strip()
        log(f"{etiqueta}: VERIFICACIÓN 3 (largo): original {len(original)} caracteres; corregido {len(corregido)} "
            f"({len(corregido) / len(original):.1%} del original); Doc antes {len(actual)}; Doc después, releído de Drive, "
            f"{len(leido)} ({len(leido) / len(original):.1%} del original, incluye la sección de correcciones).")
        log(f"{etiqueta}: VERIFICACIÓN 4 (marca): sección '{TITULO_CORRECCIONES}' "
            f"{'presente' if estado_correccion(leido) != 'sin' else 'AUSENTE'}; estado según el Doc: {estado_correccion(leido)}.")
        n_partes = len(listar(drive, m["partes"]))
        log(f"{etiqueta}: VERIFICACIÓN 5 (duplicados): Docs llamados {doc['name']!r} en {NOMBRE_TRANSCRIPCIONES}: {len(mismos)}; "
            f"archivos en {NOMBRE_TRANSCRIPCIONES}: antes {antes[0]}, después {len(en_trans)}; "
            f"archivos en {NOMBRE_PARTES}: antes {antes[1]}, después {n_partes}.")
    except Exception as e:  # noqa: BLE001
        log(f"{etiqueta}: no pude armar la verificación: {e}")


def corregir_y_reemplazar(drive, claves, m, materia, base, doc, actual, original, nombre_crudo, descripcion):
    """Corrige 'original' (el de _partes) y reemplaza el Doc en el lugar. Devuelve (resultado, resumen).

    resultado: "ok" | "sin_tiempo" (no alcanza el tiempo de la corrida) | "incompleta" (cuota, tiempo o
    corrector: el Doc queda como estaba) | "guarda" (el largo no cierra: no se reemplaza).
    Los errores de Drive (incluida la restauración tras una verificación fallida) se propagan.
    """
    etiqueta = f"[{materia}] {base}"
    glosario = glosario_de(drive, m["carpeta"], materia)
    bloques = math.ceil(len(original.split()) / palabras_por_bloque(glosario))
    if bloques * MINUTOS_POR_BLOQUE_CORRECCION > minutos_restantes():
        log(f"{etiqueta}: hacen falta ~{bloques * MINUTOS_POR_BLOQUE_CORRECCION:.0f} min y quedan "
            f"{max(0, minutos_restantes()):.0f} en esta corrida; sigue en la próxima.")
        return "sin_tiempo", None

    log(f"{etiqueta}: corrección pendiente ({descripcion}); la rehago entera desde el original.")
    corregido, correcciones, resumen = corregir_transcripcion(claves["groq"], original, glosario, base, etiqueta)
    for linea in resumen_auditoria(resumen["auditoria"]) if resumen["auditoria"] else []:
        log(f"{etiqueta}: {linea}")
    listar_auditoria_en_log(etiqueta, resumen["auditoria"])
    if not resumen["completa"]:
        log(f"{etiqueta}: no se pudo terminar ({resumen.get('motivo', 'ver arriba')}); el Doc queda como estaba. "
            f"Sigue en la próxima corrida.")
        return "incompleta", resumen
    if not corregido.strip() or not MIN_PROPORCION_CORRECCION <= len(corregido) / len(original) <= MAX_PROPORCION_CORRECCION:
        log(f"{etiqueta}: ERROR: el texto corregido mide {len(corregido)} caracteres contra {len(original)} "
            f"del original; no reemplazo el Doc.")
        return "guarda", resumen
    antes = (len(listar(drive, m["trans"])), len(listar(drive, m["partes"])))
    reemplazar_doc(drive, doc, corregido + seccion_correcciones(correcciones, resumen), actual, etiqueta)
    CORREGIDOS_EN_ESTA_CORRIDA.add((materia, base))
    informar_reemplazo(drive, m, doc, nombre_crudo, actual, original, corregido, antes, etiqueta)
    log(f"{etiqueta}: Doc corregido y reemplazado en el lugar (mismo archivo).")
    return "ok", resumen


def corregir_pendientes(drive, claves, preparadas):
    """Fase 3: corregir los Docs que quedaron sin corregir, reemplazándolos en el lugar.

    - Van de la fecha más vieja a la más nueva. Las clases nuevas de esta corrida ya
      tuvieron prioridad: se corrigieron en la fase 2, antes de llegar acá.
    - Un Doc está pendiente si no tiene la sección "Correcciones aplicadas" o si la
      tiene con bloques sin corregir (los que se cortaron por cuota).
    - Se corrige SIEMPRE desde el original de _partes ("<clase> (sin corregir)". Si
      un Doc sin sección no tiene original, se guarda antes una copia de su texto).
      Un Doc parcial sin original no se toca.
    - Todo o nada: si la cuota se acaba a mitad de una clase, ese Doc queda como estaba
      y la fase termina. Solo se reemplaza un Doc completo, no vacío, con largo entre
      85 % y 115 % del original, y después de escribirlo se verifica lo guardado.
    - Con MAX_PENDIENTES=N se corrigen a lo sumo N clases (sirve para probar con 1).
    """
    if not claves.get("groq"):
        return
    limite = int(os.environ.get("MAX_PENDIENTES") or 0) or None

    candidatos = []
    for m in preparadas:
        try:
            existentes = archivos_por_nombre(drive, m["trans"])
        except Exception as e:  # noqa: BLE001
            log(f"[{m['materia']}] no pude listar las transcripciones: {e}")
            continue
        for audio in m["audios"]:
            base = nombre_base(audio["name"])
            doc = existentes.get(base)
            if doc and doc["mimeType"] == MIME_DOC and (m["materia"], base) not in CORREGIDOS_EN_ESTA_CORRIDA:
                candidatos.append((clave_fecha(base), m["materia"], base, m, doc))
    candidatos.sort(key=lambda c: c[:3])
    if candidatos:
        log(f"Pendientes: reviso {len(candidatos)} Doc(s) de la fecha más vieja a la más nueva"
            f"{f' (máximo {limite} clase(s) a corregir)' if limite else ''}.")

    hechos = fallos = 0
    for _, materia, base, m, doc in candidatos:
        if ESTADO["sin_cuota_correccion"] or tiempo_agotado():
            break
        if limite and hechos >= limite:
            log(f"Pendientes: llegué al límite de {limite} clase(s) de esta corrida.")
            break
        etiqueta = f"[{materia}] {base}"
        try:
            actual = leer_texto(drive, doc).strip()
            estado = estado_correccion(actual)
            if estado == "completa":
                continue

            partes = archivos_por_nombre(drive, m["partes"])
            nombre_crudo = base + SUFIJO_SIN_CORREGIR
            if nombre_crudo in partes:
                original = leer_texto(drive, partes[nombre_crudo]).strip()
                if estado == "sin" and not textos_equivalentes(original, actual):
                    log(f"{etiqueta}: el Doc difiere del original de {NOMBRE_PARTES} (¿editado a mano?); no lo toco.")
                    continue
            elif estado == "sin":
                original = actual
                guardar_texto(drive, m["partes"], nombre_crudo, original)
                log(f"{etiqueta}: no había original en {NOMBRE_PARTES}; guardé una copia del Doc como '{nombre_crudo}'.")
            else:
                log(f"{etiqueta}: tiene correcciones parciales y no hay '{nombre_crudo}' en {NOMBRE_PARTES}; no lo toco.")
                continue
            if not original:
                log(f"{etiqueta}: el original está vacío; no lo toco.")
                continue

            resultado, _ = corregir_y_reemplazar(drive, claves, m, materia, base, doc, actual, original, nombre_crudo,
                                                 "sin corregir" if estado == "sin" else "corrección parcial")
            if resultado in ("sin_tiempo", "incompleta"):
                break
            if resultado == "guarda":
                continue
            hechos += 1
            fallos = 0
        except Exception as e:  # noqa: BLE001 - la fase de pendientes nunca frena la corrida
            fallos += 1
            log(f"{etiqueta}: ERROR al corregir el pendiente: {e}")
            if fallos >= 2:
                log("Pendientes: dos errores seguidos; dejo el resto para la próxima corrida.")
                break
    log(f"Pendientes: {hechos} clase(s) corregida(s) en esta corrida.")


def copiar_resumenes_spark(drive, materias):
    """Fase 0: copiar los resúmenes que Gemini Spark dejó en su carpeta privada.

    Spark escribe en una carpeta SIN compartir (así no pide confirmación):
      CLASES GRABDAS/<materia>/"dd-mm - Resumen de clase"
    Este paso los copia a CLASES/<materia>/Resúmenes de clase, que sí ve Claude.
    Se activa definiendo CARPETA_RESUMENES_SPARK_ID (ID de "CLASES GRABDAS").
    Las subcarpetas se emparejan con las materias ignorando tildes y
    mayúsculas ("ECONOMETRIA II" = "Econometría II").
    """
    origen_raiz = os.environ.get("CARPETA_RESUMENES_SPARK_ID")
    if not origen_raiz:
        return
    subcarpetas_spark = {}
    for i in listar(drive, origen_raiz):
        if i["mimeType"] == "application/vnd.google-apps.folder":
            subcarpetas_spark.setdefault(normalizar(i["name"]), []).append(i["id"])
    for materia, carpeta_id in materias:
        for origen in subcarpetas_spark.get(normalizar(materia), []):
            try:
                resumenes = [
                    i for i in listar(drive, origen)
                    if i["mimeType"] == "application/vnd.google-apps.document" and i["name"].endswith(SUFIJO_RESUMEN_SPARK)
                ]
                if not resumenes:
                    continue
                destino = subcarpeta(drive, carpeta_id, NOMBRE_RESUMENES)
                ya_estan = archivos_por_nombre(drive, destino)
                for r in resumenes:
                    if r["name"] in ya_estan:
                        continue
                    drive.files().copy(
                        fileId=r["id"],
                        body={"name": r["name"], "parents": [destino]},
                        supportsAllDrives=True,
                    ).execute(num_retries=REINTENTOS_DRIVE)
                    ya_estan[r["name"]] = r
                    log(f"[{materia}] copiado a '{NOMBRE_RESUMENES}': {r['name']}")
            except Exception as e:  # noqa: BLE001
                log(f"[{materia}] ERROR copiando resúmenes de Spark: {e}")


def buscar_materia(drive, materias, materia_buscada):
    carpeta_id = next((c for n, c in materias if normalizar(n) == normalizar(materia_buscada)), None)
    if not carpeta_id:
        sys.exit(f"Modo de prueba: no encontré la materia '{materia_buscada}'.")
    materia = next(n for n, c in materias if c == carpeta_id)
    return preparar_materia(drive, materia, carpeta_id)


def prueba_groq(drive, claves, materias, materia_buscada, audio_buscado):
    """Modo de prueba: transcribe un audio puntual SOLO con Groq (sin corrección).

    Guarda el resultado como "<audio> (groq)" en Transcripciones, sin tocar la
    transcripción existente ni usar Gemini. Si ya existe, no hace nada (borrar
    ese Doc para repetir la prueba).
    """
    if not claves.get("groq"):
        sys.exit("Modo de prueba: falta GROQ_API_KEY.")
    m = buscar_materia(drive, materias, materia_buscada)
    materia = m["materia"]
    buscado = normalizar(nombre_base(audio_buscado))
    audio = next((a for a in m["audios"] if normalizar(nombre_base(a["name"])) == buscado), None)
    if not audio:
        sys.exit(f"Modo de prueba: no encontré el audio '{audio_buscado}' en {materia}. "
                 f"Audios: {', '.join(nombre_base(a['name']) for a in m['audios']) or 'ninguno'}")

    salida = nombre_base(audio["name"]) + SUFIJO_PRUEBA_GROQ
    if salida in archivos_por_nombre(drive, m["trans"]):
        log(f"[{materia}] '{salida}' ya existe en {NOMBRE_TRANSCRIPCIONES}; no lo piso. Borralo para repetir la prueba.")
        return
    log(f"[{materia}] MODO PRUEBA: transcribo '{audio['name']}' solo con Groq -> '{salida}'.")
    inicio = time.time()
    try:
        terminado = transcribir_audio(drive, claves, materia, audio, m["trans"], m["partes"], salida=salida, solo_groq=True)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[{materia}] MODO PRUEBA: Groq no pudo transcribir: {str(e).rstrip('.')}. "
                 f"Los tramos ya hechos quedaron en {NOMBRE_PARTES}; volvé a correr la prueba para seguir.")
    if terminado:
        log(f"[{materia}] MODO PRUEBA: listo en {(time.time() - inicio) / 60:.1f} min. "
            f"Compará '{salida}' con '{nombre_base(audio['name'])}'.")


def prueba_correccion(drive, claves, materias, materia_buscada, clase_buscada):
    """Modo de prueba: corrige la transcripción existente de una clase.

    Guarda "<clase> (corregida)" en Transcripciones sin tocar el original, y
    lista en el log todas las correcciones. Si ya existe, no hace nada.
    """
    if not claves.get("groq"):
        sys.exit("Modo de prueba: falta GROQ_API_KEY.")
    m = buscar_materia(drive, materias, materia_buscada)
    materia = m["materia"]
    existentes = archivos_por_nombre(drive, m["trans"])
    buscado = normalizar(nombre_base(clase_buscada))
    base = next((n for n in existentes if normalizar(n) == buscado), None)
    if not base:
        sys.exit(f"Modo de prueba: no hay transcripción '{clase_buscada}' en {materia}/{NOMBRE_TRANSCRIPCIONES}.")
    salida = base + SUFIJO_PRUEBA_CORRECCION
    if salida in existentes:
        log(f"[{materia}] '{salida}' ya existe; no lo piso. Borralo para repetir la prueba.")
        return

    etiqueta = f"[{materia}] {salida}"
    log(f"{etiqueta}: MODO PRUEBA: corrijo la transcripción existente '{base}'.")
    texto = quitar_seccion_correcciones(leer_texto(drive, existentes[base]).strip())
    glosario = glosario_de(drive, m["carpeta"], materia)
    inicio = time.time()
    corregido, correcciones, resumen = corregir_transcripcion(claves["groq"], texto, glosario, base, etiqueta)
    guardar_texto(drive, m["trans"], salida, corregido + seccion_correcciones(correcciones, resumen), como_doc=True)
    log(f"{etiqueta}: MODO PRUEBA: guardado en {(time.time() - inicio) / 60:.1f} min. "
        f"Largo: {len(texto)} -> {len(corregido)} caracteres.")
    auditoria = resumen["auditoria"]
    listar_auditoria_en_log(etiqueta, auditoria)

    # Archivo de auditoría: local (artefacto del workflow) y en _partes
    contenido = auditoria_csv(auditoria)
    nombre_csv = f"{salida} - auditoría.csv"
    os.makedirs(CARPETA_AUDITORIA, exist_ok=True)
    Path(CARPETA_AUDITORIA, nombre_csv).write_text(contenido, encoding="utf-8")
    try:
        guardar_texto(drive, m["partes"], nombre_csv, contenido, mime="text/csv")
        log(f"{etiqueta}: auditoría guardada en {NOMBRE_TRANSCRIPCIONES}/{NOMBRE_PARTES}/{nombre_csv} "
            f"y como artefacto del workflow.")
    except Exception as e:  # noqa: BLE001
        log(f"{etiqueta}: no pude subir la auditoría a Drive ({e}); queda como artefacto del workflow.")
    for linea in resumen_auditoria(auditoria):
        log(f"{etiqueta}: {linea}")


def recorregir_clase(drive, claves, materias, materia_buscada, clase_buscada):
    """Rehace la corrección de una clase desde su original de _partes y reemplaza el Doc EN EL LUGAR.

    Sirve para volver a corregir un Doc que ya figura como completo (por ejemplo, después de cambiar
    las reglas del filtro). Necesita "<clase> (sin corregir)" en _partes. Deja en el log todas las
    correcciones propuestas (aceptadas y descartadas), la verificación del reemplazo y un CSV local
    (artefacto del workflow). Si no se puede terminar, el Doc queda como estaba y la corrida falla.
    """
    if not claves.get("groq"):
        sys.exit("Recorregir: falta GROQ_API_KEY.")
    m = buscar_materia(drive, materias, materia_buscada)
    materia = m["materia"]
    existentes = archivos_por_nombre(drive, m["trans"])
    buscado = normalizar(nombre_base(clase_buscada))
    base = next((n for n in existentes if normalizar(n) == buscado), None)
    if not base or existentes[base]["mimeType"] != MIME_DOC:
        sys.exit(f"Recorregir: no hay un Doc '{clase_buscada}' en {materia}/{NOMBRE_TRANSCRIPCIONES}.")
    doc = existentes[base]
    nombre_crudo = base + SUFIJO_SIN_CORREGIR
    partes = archivos_por_nombre(drive, m["partes"])
    if nombre_crudo not in partes:
        sys.exit(f"Recorregir: no está el original '{nombre_crudo}' en {NOMBRE_PARTES}; no se toca el Doc.")
    original = leer_texto(drive, partes[nombre_crudo]).strip()
    actual = leer_texto(drive, doc).strip()
    if not original:
        sys.exit("Recorregir: el original está vacío; no se toca el Doc.")
    etiqueta = f"[{materia}] {base}"
    log(f"{etiqueta}: RECORREGIR: estado actual del Doc: {estado_correccion(actual)}; original en {NOMBRE_PARTES}: "
        f"{len(original)} caracteres; Doc actual: {len(actual)}.")
    resultado, resumen = corregir_y_reemplazar(drive, claves, m, materia, base, doc, actual, original, nombre_crudo,
                                               "recorrección pedida a mano")
    if resumen:
        os.makedirs(CARPETA_AUDITORIA, exist_ok=True)
        Path(CARPETA_AUDITORIA, f"{base.strip()} - recorrección - auditoría.csv").write_text(
            auditoria_csv(resumen["auditoria"]), encoding="utf-8")
    if resultado != "ok":
        sys.exit(f"{etiqueta}: RECORREGIR: no se reemplazó el Doc ({resultado}).")


def main():
    drive = conectar_drive()
    materias = obtener_materias(drive, env("CARPETA_CLASES_ID"))
    claves = {"groq": os.environ.get("GROQ_API_KEY"), "gemini": os.environ.get("GEMINI_API_KEY")}

    prueba_materia = os.environ.get("PRUEBA_MATERIA", "").strip()
    prueba_audio = os.environ.get("PRUEBA_GROQ_AUDIO", "").strip()
    prueba_corr = os.environ.get("PRUEBA_CORRECCION_AUDIO", "").strip()
    recorregir = os.environ.get("RECORREGIR_AUDIO", "").strip()
    pedidos = [x for x in (prueba_audio, prueba_corr, recorregir) if x]
    if prueba_materia or pedidos:
        if not prueba_materia or len(pedidos) != 1:
            sys.exit("Modo manual: hacen falta la materia y UNO solo de estos: prueba de Groq, prueba de corrección "
                     "o recorregir en el lugar.")
        if prueba_audio:
            prueba_groq(drive, claves, materias, prueba_materia, prueba_audio)
        elif prueba_corr:
            prueba_correccion(drive, claves, materias, prueba_materia, prueba_corr)
        else:
            recorregir_clase(drive, claves, materias, prueba_materia, recorregir)
        log("Fin de la corrida (modo manual).")
        return

    claves["gemini"] = env("GEMINI_API_KEY")
    if not claves["groq"]:
        ESTADO["sin_cuota_groq"] = True
        log("AVISO: falta GROQ_API_KEY; se transcribe solo con Gemini.")
    log(f"Materias: {', '.join(m for m, _ in materias) or 'ninguna'}"
        f" | transcripción: {'Groq, con Gemini de respaldo' if claves['groq'] else 'Gemini'}"
        f" | resúmenes en este script: {'sí' if HACER_RESUMENES else 'no (los hace Gemini Spark)'}")

    # Fase 0: copiar resúmenes de Spark (rápido, no usa IA)
    copiar_resumenes_spark(drive, materias)

    preparadas = []
    for nombre, carpeta_id in materias:
        try:
            preparadas.append(preparar_materia(drive, nombre, carpeta_id))
        except Exception as e:  # noqa: BLE001
            log(f"[{nombre}] ERROR al leer la materia: {e}. La salteo en esta corrida.")

    # Fase 1: resúmenes pendientes (solo si están activados en este script)
    for m in (preparadas if HACER_RESUMENES else []):
        try:
            resumir_pendientes(drive, claves["gemini"], m)
        except Exception as e:  # noqa: BLE001
            log(f"[{m['materia']}] ERROR inesperado en resúmenes: {e}")

    # Fase 2: transcripciones pendientes
    for m in preparadas:
        if tiempo_agotado():
            log("Tiempo de la corrida agotado; lo pendiente sigue en la próxima.")
            break
        try:
            transcribir_pendientes(drive, claves, m)
        except Exception as e:  # noqa: BLE001
            log(f"[{m['materia']}] ERROR inesperado en transcripciones: {e}")

    # Fase 3: corregir lo que quedó sin corregir (después de las clases nuevas, que tienen prioridad)
    if claves["groq"] and not tiempo_agotado():
        try:
            corregir_pendientes(drive, claves, preparadas)
        except Exception as e:  # noqa: BLE001
            log(f"ERROR inesperado en la fase de pendientes: {e}")

    log("Fin de la corrida.")


if __name__ == "__main__":
    main()
