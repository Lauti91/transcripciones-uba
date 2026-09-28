"""
Transcripciones UBA: Drive -> Groq (Whisper) / Gemini -> Drive

Recorre las materias dentro de la carpeta CLASES de Google Drive (carpetas o
accesos directos). Para cada audio de clase:

  1. Si no tiene transcripción: lo baja, lo parte en tramos de hasta 45 minutos
     con ffmpeg y transcribe cada tramo con Groq (whisper-large-v3). Si Groq
     falla o no tiene cuota, usa gemini-3.5-transcribe como respaldo (acepta
     hasta ~50 min por pedido). Cada tramo terminado se guarda en
     Transcripciones/_partes, así que si una corrida se corta, la siguiente
     retoma desde el tramo que falta. Al final une los tramos y guarda la
     transcripción con el mismo nombre que el audio.
  2. Si tiene transcripción pero no resumen: genera un resumen reestructurado
     con un modelo de texto de Gemini y lo guarda como "<nombre> - resumen".

Transcripción y resumen se guardan como Google Docs en la subcarpeta
"Transcripciones" de cada materia.

Variables de entorno necesarias (en GitHub van como secretos):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
  GEMINI_API_KEY, CARPETA_CLASES_ID
Opcionales:
  GROQ_API_KEY (sin ella se transcribe solo con Gemini),
  CARPETA_RESUMENES_SPARK_ID (sin ella se saltea la fase 0),
  PRUEBA_GROQ_MATERIA + PRUEBA_GROQ_AUDIO (modo de prueba, ver prueba_groq).
"""

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
ESTADO = {"sin_cuota_gemini": False, "sin_cuota_groq": False}


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
    return buf.getvalue().decode("utf-8-sig")


def guardar_texto(drive, carpeta_id, nombre, texto, como_doc=False):
    """Guarda un texto en Drive; con como_doc=True lo convierte en Google Doc."""
    media = MediaIoBaseUpload(io.BytesIO(texto.encode("utf-8")), mimetype="text/plain", resumable=True)
    tipo = "application/vnd.google-apps.document" if como_doc else "text/plain"
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


def normalizar(nombre):
    """Para comparar nombres ignorando tildes, mayúsculas y espacios de más."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", nombre) if not unicodedata.combining(c)
    )
    return " ".join(sin_tildes.casefold().split())


# ---------------------------------------------------------------------------
# Procesamiento
# ---------------------------------------------------------------------------

def transcribir_audio(drive, claves, materia, audio, carpeta_trans, carpeta_partes, salida=None, solo_groq=False):
    """Transcribe un audio por tramos y guarda el Doc 'salida' (por defecto, el nombre del audio)."""
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

    # Todos los tramos listos: unir, guardar y limpiar
    partes = archivos_por_nombre(drive, carpeta_partes)
    textos = [leer_texto(drive, partes[n]).strip() for n in nombres_partes]
    guardar_texto(drive, carpeta_trans, salida, "\n\n".join(textos), como_doc=True)
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
    return {"materia": materia, "trans": carpeta_trans, "partes": carpeta_partes, "audios": audios}


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
            if not transcribir_audio(drive, claves, m["materia"], audio, m["trans"], m["partes"]):
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


def prueba_groq(drive, claves, materias, materia_buscada, audio_buscado):
    """Modo de prueba: transcribe un audio puntual SOLO con Groq.

    Guarda el resultado como "<audio> (groq)" en Transcripciones, sin tocar la
    transcripción existente ni usar Gemini. Si ya existe, no hace nada (borrar
    ese Doc para repetir la prueba).
    """
    if not claves.get("groq"):
        sys.exit("Modo de prueba: falta GROQ_API_KEY.")
    carpeta_id = next((c for n, c in materias if normalizar(n) == normalizar(materia_buscada)), None)
    if not carpeta_id:
        sys.exit(f"Modo de prueba: no encontré la materia '{materia_buscada}'.")
    materia = next(n for n, c in materias if c == carpeta_id)
    m = preparar_materia(drive, materia, carpeta_id)
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


def main():
    drive = conectar_drive()
    materias = obtener_materias(drive, env("CARPETA_CLASES_ID"))
    claves = {"groq": os.environ.get("GROQ_API_KEY"), "gemini": os.environ.get("GEMINI_API_KEY")}

    prueba_materia = os.environ.get("PRUEBA_GROQ_MATERIA", "").strip()
    prueba_audio = os.environ.get("PRUEBA_GROQ_AUDIO", "").strip()
    if prueba_materia or prueba_audio:
        if not (prueba_materia and prueba_audio):
            sys.exit("Modo de prueba: hacen falta la materia y el audio.")
        prueba_groq(drive, claves, materias, prueba_materia, prueba_audio)
        log("Fin de la corrida (modo prueba).")
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

    log("Fin de la corrida.")


if __name__ == "__main__":
    main()
