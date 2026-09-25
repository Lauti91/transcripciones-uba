"""
Transcripciones UBA: Drive -> Gemini -> Drive

Recorre las materias dentro de la carpeta CLASES de Google Drive (carpetas o
accesos directos). Para cada audio de clase:

  1. Si no tiene transcripción: lo baja, lo parte en tramos de 40 minutos con
     ffmpeg y transcribe cada tramo con gemini-3.5-transcribe (que acepta
     hasta ~50 min por pedido). Cada tramo terminado se guarda en
     Transcripciones/_partes, así que si una corrida se corta, la siguiente
     retoma desde el tramo que falta. Al final une los tramos y guarda la
     transcripción con el mismo nombre que el audio.
  2. Si tiene transcripción pero no resumen: genera un resumen reestructurado
     con un modelo de texto de Gemini y lo guarda como "<nombre> - resumen".

Todo se guarda en la subcarpeta "Transcripciones" de cada materia.

Variables de entorno necesarias (en GitHub van como secretos):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
  GEMINI_API_KEY, CARPETA_CLASES_ID
"""

import io
import json
import math
import os
import subprocess
import sys
import tempfile
import time
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
SUFIJO_RESUMEN = " - resumen"

MODELO_TRANSCRIPCION = "gemini-3.5-transcribe"
# Para el resumen: si uno está saturado, se prueba el siguiente.
MODELOS_RESUMEN = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]

# 40 min de audio ≈ 76.800 tokens (32 por segundo): margen cómodo bajo el
# límite de 98.304 del modelo de transcripción.
SEGUNDOS_POR_TRAMO = 40 * 60

# Reintentos ante saturación (503) o límites (429): espera creciente.
MAX_REINTENTOS = 8
ESPERA_INICIAL_S = 30
ESPERA_MAXIMA_S = 300

# Tiempo máximo de trabajo por corrida. Pasado esto no se empieza nada
# nuevo; lo pendiente sigue en la próxima corrida.
MAX_MINUTOS_CORRIDA = 45

MIN_CARACTERES = 200

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
        ).execute()
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
    ).execute()
    return nueva["id"]


def archivos_por_nombre(drive, carpeta_id):
    return {i["name"]: i for i in listar(drive, carpeta_id) if i["mimeType"] != "application/vnd.google-apps.folder"}


def bajar(drive, archivo_id, destino):
    with open(destino, "wb") as f:
        req = drive.files().get_media(fileId=archivo_id, supportsAllDrives=True)
        dl = MediaIoBaseDownload(f, req, chunksize=32 * 1024 * 1024)
        terminado = False
        while not terminado:
            _, terminado = dl.next_chunk()


def leer_texto(drive, archivo_id):
    buf = io.BytesIO()
    req = drive.files().get_media(fileId=archivo_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req)
    terminado = False
    while not terminado:
        _, terminado = dl.next_chunk()
    return buf.getvalue().decode("utf-8")


def guardar_texto(drive, carpeta_id, nombre, texto):
    media = MediaIoBaseUpload(io.BytesIO(texto.encode("utf-8")), mimetype="text/plain", resumable=False)
    drive.files().create(
        body={"name": nombre, "parents": [carpeta_id], "mimeType": "text/plain"},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()


def a_papelera(drive, archivo_id):
    drive.files().update(fileId=archivo_id, body={"trashed": True}, supportsAllDrives=True).execute()


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
            log(f"{descripcion}: {e} -> reintento {intento}/{MAX_REINTENTOS - 1} en {espera}s")
            time.sleep(espera)
            espera = min(espera * 2, ESPERA_MAXIMA_S)


class ErrorReintentable(Exception):
    pass


def revisar_respuesta(r, contexto):
    if r.status_code == 200:
        return
    detalle = r.text[:500]
    if r.status_code == 429 or r.status_code >= 500:
        raise ErrorReintentable(f"{contexto} respondió {r.status_code}")
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
        r = requests.post(
            f"{GEMINI_BASE}/v1beta/models/{modelo}:generateContent?key={api_key}",
            json={"contents": [{"parts": parts}]},
            timeout=900,
        )
        revisar_respuesta(r, f"{modelo} ({contexto})")
        texto, fin = extraer_texto(r.json())
        if len(texto.strip()) < MIN_CARACTERES:
            # Respuesta vacía: suele ser algo transitorio, se reintenta.
            raise ErrorReintentable(f"{modelo} devolvió {len(texto.strip())} caracteres (finishReason: {fin})")
        if fin == "MAX_TOKENS":
            log(f"AVISO: {modelo} cortó la salida por límite de tokens ({contexto}); puede estar incompleta.")
        return texto

    return con_reintentos(f"{modelo} ({contexto})", _llamar)


# ---------------------------------------------------------------------------
# Procesamiento
# ---------------------------------------------------------------------------

def transcribir_audio(drive, api_key, materia, audio, carpeta_trans, carpeta_partes):
    base = nombre_base(audio["name"])
    etiqueta = f"[{materia}] {base}"

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "audio_original"
        log(f"{etiqueta}: bajando audio ({int(audio.get('size', 0)) // (1024 * 1024)} MB)...")
        bajar(drive, audio["id"], ruta)

        duracion = duracion_segundos(ruta)
        total = max(1, math.ceil(duracion / SEGUNDOS_POR_TRAMO))
        log(f"{etiqueta}: {duracion / 60:.0f} min -> {total} tramo(s) de hasta {SEGUNDOS_POR_TRAMO // 60} min.")

        partes_existentes = archivos_por_nombre(drive, carpeta_partes)
        nombres_partes = [f"{base} - parte {i + 1} de {total}" for i in range(total)]

        for i, nombre_parte in enumerate(nombres_partes):
            if nombre_parte in partes_existentes:
                log(f"{etiqueta}: tramo {i + 1}/{total} ya estaba hecho.")
                continue
            if tiempo_agotado():
                log(f"{etiqueta}: se acabó el tiempo de esta corrida; sigue en la próxima.")
                return False

            tramo = Path(tmp) / f"tramo_{i + 1}.mp3"
            extraer_tramo(ruta, i * SEGUNDOS_POR_TRAMO, SEGUNDOS_POR_TRAMO, tramo)
            archivo_gemini = subir_a_gemini(api_key, tramo)
            try:
                texto = generar(
                    api_key,
                    MODELO_TRANSCRIPCION,
                    [{"text": PROMPT_TRANSCRIBIR},
                     {"file_data": {"mime_type": archivo_gemini["mimeType"], "file_uri": archivo_gemini["uri"]}}],
                    f"{base}, tramo {i + 1}/{total}",
                )
            finally:
                borrar_de_gemini(api_key, archivo_gemini["name"])

            guardar_texto(drive, carpeta_partes, nombre_parte, texto)
            partes_existentes[nombre_parte] = True
            log(f"{etiqueta}: tramo {i + 1}/{total} OK ({len(texto)} caracteres).")

    # Todos los tramos listos: unir, guardar y limpiar
    partes = archivos_por_nombre(drive, carpeta_partes)
    textos = [leer_texto(drive, partes[n]["id"]).strip() for n in nombres_partes]
    guardar_texto(drive, carpeta_trans, base, "\n\n".join(textos))
    for n in nombres_partes:
        a_papelera(drive, partes[n]["id"])
    log(f"{etiqueta}: transcripción completa guardada.")
    return True


def resumir(drive, api_key, materia, base, transcripcion_id, carpeta_trans):
    etiqueta = f"[{materia}] {base}"
    texto = leer_texto(drive, transcripcion_id)
    prompt = PROMPT_RESUMEN.format(materia=materia, clase=base, transcripcion=texto)

    ultimo_error = None
    for modelo in MODELOS_RESUMEN:
        if tiempo_agotado():
            break
        try:
            resumen = generar(api_key, modelo, [{"text": prompt}], f"resumen de {base}")
            guardar_texto(drive, carpeta_trans, base + SUFIJO_RESUMEN, resumen)
            log(f"{etiqueta}: resumen guardado (con {modelo}).")
            return
        except (ErrorReintentable, RuntimeError) as e:
            ultimo_error = e
            log(f"{etiqueta}: {modelo} no pudo con el resumen ({e}); pruebo el siguiente.")
    log(f"{etiqueta}: resumen pendiente para la próxima corrida. Último error: {ultimo_error}")


def procesar_materia(drive, api_key, materia, carpeta_id):
    carpeta_trans = subcarpeta(drive, carpeta_id, NOMBRE_TRANSCRIPCIONES)
    carpeta_partes = subcarpeta(drive, carpeta_trans, NOMBRE_PARTES)

    audios = sorted(
        (i for i in listar(drive, carpeta_id) if i["mimeType"] != "application/vnd.google-apps.folder" and es_audio(i)),
        key=lambda i: i["name"],
    )
    for item in listar(drive, carpeta_id):
        if item["mimeType"] not in ("application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut") and not es_audio(item):
            log(f"[{materia}] salteo '{item['name']}' (tipo {item['mimeType']}, no parece audio).")

    for audio in audios:
        if tiempo_agotado():
            return
        base = nombre_base(audio["name"])
        existentes = archivos_por_nombre(drive, carpeta_trans)

        if base not in existentes:
            try:
                if not transcribir_audio(drive, api_key, materia, audio, carpeta_trans, carpeta_partes):
                    return
            except Exception as e:  # noqa: BLE001 - seguir con el resto aunque uno falle
                log(f"[{materia}] {base}: ERROR al transcribir: {e}")
                continue
            existentes = archivos_por_nombre(drive, carpeta_trans)

        if base + SUFIJO_RESUMEN not in existentes and base in existentes:
            try:
                resumir(drive, api_key, materia, base, existentes[base]["id"], carpeta_trans)
            except Exception as e:  # noqa: BLE001
                log(f"[{materia}] {base}: ERROR al resumir: {e}")


def main():
    api_key = env("GEMINI_API_KEY")
    drive = conectar_drive()
    materias = obtener_materias(drive, env("CARPETA_CLASES_ID"))
    log(f"Materias: {', '.join(m for m, _ in materias) or 'ninguna'}")

    for nombre, carpeta_id in materias:
        if tiempo_agotado():
            log("Tiempo de la corrida agotado; lo pendiente sigue en la próxima.")
            break
        procesar_materia(drive, api_key, nombre, carpeta_id)

    log("Fin de la corrida.")


if __name__ == "__main__":
    main()
