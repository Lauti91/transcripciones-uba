"""Pruebas de la segunda persona: key de Groq por materia, cuota por key, alternancia y fase 0.

Sin red ni ffmpeg: Drive, Groq y Gemini están simulados. Se corre con
    python tests/test_segunda_persona.py
(el workflow no lo corre solo: es de uso manual al tocar este código).
"""
import contextlib
import importlib.util
import io
import itertools
import os
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
KEY_P, KEY_A = "gsk_PROPIA_0123456789", "gsk_AMIGA_9876543210"
ENV_LIMPIAR = ["GROQ_API_KEY", "GROQ_API_KEY_AMIGA", "MATERIAS_AMIGA", "GEMINI_API_KEY", "CARPETA_RESUMENES_SPARK_ID",
               "PRUEBA_MATERIA", "PRUEBA_GROQ_AUDIO", "PRUEBA_CORRECCION_AUDIO", "CARPETA_CLASES_ID"]
_ids = itertools.count(1)


class Resp:
    def __init__(self, status, text="", js=None, headers=None):
        self.status_code, self.text, self._js, self.headers = status, text, js, headers or {}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


def error(status, mensaje="", headers=None):
    return Resp(status, text=mensaje, js={"error": {"message": mensaje}}, headers=headers)


TPD = error(429, "Rate limit reached for model `x` on tokens per day (TPD): Limit 200000, Used 199999. "
                 "Please try again in 2h0m0s.")
WHISPER_TPD = error(429, "Rate limit reached on audio seconds per day (ASPD). Please try again in 2h0m0s.")


class FakeDrive:
    """Drive en memoria: lo justo para listar, crear, leer, copiar y papelera."""

    def __init__(self):
        self.files = {}

    def add(self, parent, name, mime, content=""):
        fid = f"id{next(_ids)}"
        self.files[fid] = dict(id=fid, name=name, mimeType=mime, parent=parent, content=content, trashed=False,
                               size="1000")
        return fid

    def children(self, parent):
        return [f for f in self.files.values() if f["parent"] == parent and not f["trashed"]]

    def names(self, parent):
        return sorted(f["name"] for f in self.children(parent))

    def folder(self, parent, name):
        return next(f["id"] for f in self.children(parent) if f["mimeType"] == FOLDER and f["name"] == name)

    def doc(self, parent, name):
        return next((f for f in self.children(parent) if f["name"] == name), None)

    # API de googleapiclient que usa el script (solo copy y update/trashed)
    def files_api(self):
        drive = self

        class Op:
            def __init__(self, fn):
                self.fn = fn

            def execute(self, num_retries=0):
                return self.fn()

        class F:
            def copy(self, fileId, body, supportsAllDrives=None):
                def hacer():
                    origen = drive.files[fileId]
                    nuevo = drive.add(body["parents"][0], body["name"], origen["mimeType"], origen["content"])
                    return {"id": nuevo}
                return Op(hacer)

            def update(self, fileId, body=None, supportsAllDrives=None, **kw):
                def hacer():
                    if body and body.get("trashed"):
                        drive.files[fileId]["trashed"] = True
                    return {}
                return Op(hacer)

        return F()


class Mundo:
    """Árbol de Drive de prueba + APIs simuladas. Registra todo lo que pasa."""

    def __init__(self):
        self.drive = FakeDrive()
        self.clases = self.drive.add(None, "CLASES", FOLDER)
        self.spark = self.drive.add(None, "SPARK", FOLDER)
        self.llamadas = []          # (tipo, key, detalle) en orden
        self.planes = {}            # (tipo, key) -> lista de respuestas (None = OK)
        self.gemini_ok = True

    def materia(self, nombre, audios=(), glosario=None):
        carpeta = self.drive.add(self.clases, nombre, FOLDER)
        for a in audios:
            self.drive.add(carpeta, a, "audio/mp4")
        if glosario:
            ctx = self.drive.add(carpeta, "Contexto", FOLDER)
            self.drive.add(ctx, "Glosario.txt", "text/plain", glosario)
        return carpeta

    def transcripciones(self, carpeta):
        return self.drive.folder(carpeta, "Transcripciones")

    def plan(self, tipo, key, *respuestas):
        self.planes.setdefault((tipo, key), []).extend(respuestas)

    def _siguiente(self, tipo, key):
        cola = self.planes.get((tipo, key))
        return cola.pop(0) if cola else None

    # --- requests simulado ---------------------------------------------------
    def post(self, url, headers=None, files=None, data=None, json=None, timeout=None):
        auth = (headers or {}).get("Authorization", "")
        key = auth.replace("Bearer ", "")
        if "chat/completions" in url:
            self.llamadas.append(("chat", key, None))
            r = self._siguiente("chat", key)
            if r is not None:
                return r
            import json as _j
            import re
            bloque = re.search(r"<<<\n(.*)\n>>>", json["messages"][1]["content"], re.S).group(1)
            contenido = _j.dumps({"texto": bloque, "correcciones": []})
            return Resp(200, js={"choices": [{"message": {"content": contenido}}]},
                        headers={"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "1s"})
        if "api.groq.com" in url:
            self.llamadas.append(("whisper", key, None))
            r = self._siguiente("whisper", key)
            if r is not None:
                return r
            n = sum(1 for c in self.llamadas if c[0] == "whisper")
            return Resp(200, " ".join(f"Frase {n}-{i} de la clase." for i in range(40)))
        if "upload/v1beta/files" in url:
            return Resp(200, headers={"X-Goog-Upload-URL": "https://upload/x"})
        if url == "https://upload/x":
            return Resp(200, js={"file": {"name": "files/abc"}})
        if ":generateContent" in url:
            self.llamadas.append(("gemini", None, None))
            n = sum(1 for c in self.llamadas if c[0] == "gemini")
            texto = " ".join(f"Gemini {n}-{i} de la clase." for i in range(40))
            return Resp(200, js={"candidates": [{"content": {"parts": [{"audioTranscription": {"text": texto}}]},
                                                 "finishReason": "STOP"}]})
        raise AssertionError(url)

    def get(self, url, timeout=None):
        return Resp(200, js={"name": "files/abc", "state": "ACTIVE", "mimeType": "audio/mpeg", "uri": "u"})

    def delete(self, url, timeout=None):
        return Resp(200)

    def quien(self, tipo):
        """Etiquetas de las keys usadas en las llamadas de ese tipo, en orden."""
        nombres = {KEY_P: "propia", KEY_A: "amiga", None: "-"}
        return [nombres[c[1]] for c in self.llamadas if c[0] == tipo]


def cargar_modulo(ruta=None):
    ruta = Path(ruta or REPO / "transcribir.py")
    spec = importlib.util.spec_from_file_location(f"transcribir_{next(_ids)}", ruta)
    modulo = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO))
    spec.loader.exec_module(modulo)
    return modulo


def correr(mundo, env, ruta=None, semilla=1):
    """Corre main() con el mundo simulado. Devuelve (log, módulo)."""
    for k in ENV_LIMPIAR:
        os.environ.pop(k, None)
    os.environ.update({"GEMINI_API_KEY": "gemini-key-0000000", "CARPETA_CLASES_ID": mundo.clases})
    os.environ.update(env)
    t = cargar_modulo(ruta)
    drive = mundo.drive
    t.ESPERA_INICIAL_S = 0
    t.PAUSA_ENTRE_TRAMOS_S = 0
    t.conectar_drive = lambda: type("D", (), {"files": staticmethod(drive.files_api)})()
    t.listar = lambda d, cid: [dict(f, shortcutDetails={}) for f in drive.children(cid)]

    def subcarpeta(d, padre, nombre):
        for f in drive.children(padre):
            if f["mimeType"] == FOLDER and f["name"] == nombre:
                return f["id"]
        return drive.add(padre, nombre, FOLDER)

    t.subcarpeta = subcarpeta
    t.bajar = lambda d, fid, dest: Path(dest).write_bytes(b"audio")
    t.duracion_segundos = lambda ruta_audio: 600.0
    t.extraer_tramo = lambda ruta_audio, desde, dur, dest: Path(dest).write_bytes(b"mp3")
    t.leer_texto = lambda d, item: drive.files[item["id"]]["content"]
    t.guardar_texto = lambda d, cid, nombre, texto, como_doc=False, mime="text/plain": drive.add(
        cid, nombre, DOC if como_doc else mime, texto)
    t.a_papelera = lambda d, fid: drive.files[fid].update(trashed=True)
    t.requests.post, t.requests.get, t.requests.delete = mundo.post, mundo.get, mundo.delete
    t.time.sleep = lambda s: None
    random.seed(semilla)
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        t.main()
    return salida.getvalue(), t


# ---------------------------------------------------------------------------
# Mini marco de pruebas
# ---------------------------------------------------------------------------

FALLAS = []


def chequear(condicion, descripcion, detalle=""):
    if condicion:
        print(f"  OK  {descripcion}")
    else:
        FALLAS.append(descripcion)
        print(f"  FALLA  {descripcion} {detalle}")


def sin_secretos(log):
    return not any(k in log for k in (KEY_P, KEY_A, "gemini-key-0000000"))


def seccion(doc):
    return doc["content"] if doc else ""


def prueba_a1_dos_materias_cada_una_con_su_key():
    print("a1) dos materias, cada una con su key")
    m = Mundo()
    propia = m.materia("DESARROLLO", ["10-09.m4a"])
    amiga = m.materia("Historia Económica", ["11-09.m4a"])
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": " historia economica , otra"})
    chequear(m.drive.doc(m.transcripciones(propia), "10-09") is not None, "Doc 10-09 en la carpeta propia")
    chequear(m.drive.doc(m.transcripciones(amiga), "11-09") is not None, "Doc 11-09 en la carpeta de ella")
    chequear(m.quien("whisper") in (["propia", "amiga"], ["amiga", "propia"]), "una transcripción con cada key", m.quien("whisper"))
    chequear(sorted(set(m.quien("chat"))) == ["amiga", "propia"], "la corrección usa la key de cada una", m.quien("chat"))
    chequear("tramo 1/1 OK con groq (key amiga)" in log and "tramo 1/1 OK con groq (key propia)" in log,
             "el log identifica la key por etiqueta")
    chequear(sin_secretos(log), "ninguna key (ni parte) aparece en el log")
    chequear("MATERIAS_AMIGA menciona 'otra' pero no hay" in log, "avisa un nombre de MATERIAS_AMIGA sin carpeta")
    chequear("Correcciones aplicadas" in seccion(m.drive.doc(m.transcripciones(amiga), "11-09")), "Doc corregido")
    chequear(not any(c[0] == "gemini" for c in m.llamadas), "sin Gemini")


def prueba_a2_key_de_ella_faltante():
    print("a2) key de ella faltante")
    m = Mundo()
    m.materia("DESARROLLO", ["10-09.m4a"])
    amiga = m.materia("Historia Económica", ["11-09.m4a"])
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(set(m.quien("whisper") + m.quien("chat")) == {"propia"}, "todo con la key propia")
    chequear(log.count("fallback a key propia por falta GROQ_API_KEY_AMIGA") == 1, "log: fallback por key faltante (una vez)")
    chequear(m.drive.doc(m.transcripciones(amiga), "11-09") is not None, "se transcribió igual")
    chequear(sin_secretos(log), "sin secretos en el log")


def prueba_a3_key_de_ella_con_401():
    print("a3) key de ella con 401 / 403")
    # whisper rechazado
    m = Mundo()
    amiga = m.materia("Historia Económica", ["11-09.m4a", "12-09.m4a"])
    m.plan("whisper", KEY_A, error(401, "Invalid API Key"), error(401, "Invalid API Key"), error(401, "Invalid API Key"))
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(m.quien("whisper").count("amiga") == 1, "la key rechazada se prueba una sola vez", m.quien("whisper"))
    chequear(m.quien("whisper").count("propia") == 2 and set(m.quien("chat")) == {"propia"},
             "el resto (transcribir y corregir) va con la propia", (m.quien("whisper"), m.quien("chat")))
    chequear(log.count("fallback a key propia por rechazo 401 de la key de la amiga") == 1, "log: fallback por 401 (una vez)")
    chequear(len(m.drive.names(m.transcripciones(amiga))) == 3, "los dos Docs quedaron (más _partes)", m.drive.names(m.transcripciones(amiga)))
    chequear(sin_secretos(log), "sin secretos en el log")
    # 403 en la corrección, whisper bien
    m = Mundo()
    amiga = m.materia("Historia Económica", ["11-09.m4a"])
    m.plan("chat", KEY_A, error(403, "forbidden"))
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    doc = m.drive.doc(m.transcripciones(amiga), "11-09")
    chequear(m.quien("whisper") == ["amiga"], "la transcripción sí usó la key de ella")
    chequear(m.quien("chat") == ["amiga", "propia"], "la corrección reintentó el bloque con la propia", m.quien("chat"))
    chequear("sin corregir: 0" in seccion(doc), "el Doc quedó corregido completo", seccion(doc)[-200:])
    chequear("fallback a key propia por rechazo 403" in log, "log: fallback por 403")
    # 401 y sin key propia: sigue con Gemini, sin trabarse
    m = Mundo()
    amiga = m.materia("Historia Económica", ["11-09.m4a"])
    m.plan("whisper", KEY_A, error(401, "Invalid API Key"))
    log, _ = correr(m, {"GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(m.drive.doc(m.transcripciones(amiga), "11-09") is not None, "sin key propia: transcribe con Gemini")
    chequear(any(c[0] == "gemini" for c in m.llamadas), "usó Gemini")


def prueba_a4_cuota_agotada_en_una_sola_key():
    print("a4) cuota agotada en una sola key: la otra sigue")
    # corrección
    m = Mundo()
    propia = m.materia("DESARROLLO", ["10-09.m4a", "11-09.m4a"])
    amiga = m.materia("Historia Económica", ["10-09.m4a", "11-09.m4a"])
    m.plan("chat", KEY_A, TPD)
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chat = m.quien("chat")
    chequear(chat.count("amiga") == 1, "a ella se le para la corrección tras el 429 diario (un solo pedido)", chat)
    chequear(chat.count("propia") >= 2, "la propia sigue corrigiendo", chat)
    for nombre in ("10-09", "11-09"):
        chequear("sin corregir: 0" in seccion(m.drive.doc(m.transcripciones(propia), nombre)), f"propia {nombre}: corregida")
        chequear("sin corregir: 0" not in seccion(m.drive.doc(m.transcripciones(amiga), nombre)), f"amiga {nombre}: guardada sin corregir")
    chequear(m.quien("whisper").count("amiga") == 2, "ella sigue transcribiendo con su key")
    # transcripción
    m = Mundo()
    propia = m.materia("DESARROLLO", ["10-09.m4a", "11-09.m4a"])
    amiga = m.materia("Historia Económica", ["10-09.m4a", "11-09.m4a"])
    m.plan("whisper", KEY_A, WHISPER_TPD)
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(m.quien("whisper").count("amiga") == 1, "Groq de ella: un solo intento tras agotar la cuota", m.quien("whisper"))
    chequear(m.quien("whisper").count("propia") == 2, "Groq propio sigue con sus dos audios")
    chequear(m.quien("gemini") == ["-", "-"], "los dos audios de ella pasaron a Gemini", m.quien("gemini"))
    chequear(len(m.drive.names(m.transcripciones(amiga))) >= 3 and len(m.drive.names(m.transcripciones(propia))) >= 3, "todos los Docs hechos")
    # Gemini agotado + Groq de ella agotado: ella se frena, la propia sigue
    m = Mundo()
    propia = m.materia("DESARROLLO", ["10-09.m4a"])
    amiga = m.materia("Historia Económica", ["10-09.m4a", "11-09.m4a"])
    m.plan("whisper", KEY_A, WHISPER_TPD)
    from_gemini = error(429, "quota", None)
    m.post_original = m.post

    def post_sin_gemini(url, **kw):
        if ":generateContent" in url:
            m.llamadas.append(("gemini", None, None))
            return Resp(429, js={"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                                          "violations": [{"quotaId": "GenerateRequestsPerDayPerProject"}]}]}})
        return m.post_original(url, **kw)

    m.post = post_sin_gemini
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(m.drive.doc(m.transcripciones(propia), "10-09") is not None, "la propia se transcribió igual")
    chequear(m.drive.doc(m.transcripciones(amiga), "10-09") is None and m.drive.doc(m.transcripciones(amiga), "11-09") is None,
             "ella queda esperando (sin Groq ni Gemini)")


def prueba_a5_alternancia():
    print("a5) alternancia de audios")
    for semilla in range(12):
        m = Mundo()
        m.materia("DESARROLLO", ["01-09.m4a", "02-09.m4a", "03-09.m4a"])
        m.materia("Historia Económica", ["04-09.m4a", "05-09.m4a"])
        correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"}, semilla=semilla)
        orden = [p[0].upper() for p in m.quien("whisper")]   # P / A
        ok = "".join(orden) in ("PAPAP", "APAPP")
        if not ok:
            chequear(False, f"semilla {semilla}: orden alternado", "".join(orden))
            break
    else:
        chequear(True, "con 12 semillas: siempre alterna (PAPAP o APAPP)")
    # solo de una persona: el orden es el de siempre (por materia, mezclado dentro de cada una)
    m = Mundo()
    m.materia("DESARROLLO", ["01-09.m4a", "02-09.m4a"])
    m.materia("Macro", ["03-09.m4a", "04-09.m4a"])
    log, t = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chequear("las dos personas" not in log, "sin pendientes de ella no hay alternancia")
    chequear(t.intercalar([1, 2, 3], []) == [1, 2, 3] and t.intercalar([], [4, 5]) == [4, 5], "intercalar: una sola lista no cambia")
    random.seed(3)
    r = t.intercalar(["p1", "p2", "p3"], ["a1"])
    chequear(r in (["p1", "a1", "p2", "p3"], ["a1", "p1", "p2", "p3"]), "intercalar: alterna y sigue con lo que sobra", r)


def prueba_a6_fase0():
    print("a6) fase 0: nombres normalizados y colisiones")
    m = Mundo()
    eco = m.materia("Econometría II")
    d = m.drive
    carpeta_spark = d.add(m.spark, "ECONOMETRIA  ii", FOLDER)
    d.add(carpeta_spark, "03-09 - Resumen de clase", DOC, "resumen")
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "CARPETA_RESUMENES_SPARK_ID": m.spark})
    destino = d.folder(eco, "Resúmenes de clase")
    chequear(d.names(destino) == ["03-09 - Resumen de clase"], "empareja con otras tildes/mayúsculas/espacios")
    chequear(d.names(carpeta_spark) == ["03-09 - Resumen de clase"], "el resumen se COPIA (queda en Spark)")
    # colisión del lado de Spark
    m = Mundo()
    hist = m.materia("Historia")
    d = m.drive
    for nombre in ("Historia", "HISTORIA"):
        c = d.add(m.spark, nombre, FOLDER)
        d.add(c, "03-09 - Resumen de clase", DOC, "x")
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "CARPETA_RESUMENES_SPARK_ID": m.spark})
    chequear(not any(f["name"] == "Resúmenes de clase" for f in d.children(hist)), "colisión en Spark: no copia nada")
    chequear("2 carpetas de Spark que normalizan" in log, "colisión en Spark: avisa en el log")
    # colisión del lado de CLASES
    m = Mundo()
    a, b = m.materia("Macro"), m.materia("macro ")
    d = m.drive
    c = d.add(m.spark, "Macro", FOLDER)
    d.add(c, "03-09 - Resumen de clase", DOC, "x")
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "CARPETA_RESUMENES_SPARK_ID": m.spark})
    chequear(not any(f["name"] == "Resúmenes de clase" for f in d.children(a) + d.children(b)), "colisión en CLASES: no copia nada")
    chequear(log.count("no copio resúmenes de Spark para ninguna") == 1, "colisión en CLASES: un solo aviso")


def prueba_a7_identidad_de_la_key():
    print("a7) estado por identidad de la key, no por etiqueta")
    t = cargar_modulo()
    p, a = t.ClaveGroq("propia", "mismo-valor-de-key-1"), t.ClaveGroq("amiga", "mismo-valor-de-key-1")
    chequear(p.estado is a.estado and p.identidad == a.identidad, "dos etiquetas con el mismo valor comparten estado")
    chequear("mismo-valor" not in repr(p) and "mismo-valor" not in p.identidad, "ni repr ni identidad muestran el valor")
    otra = t.ClaveGroq("amiga", "otro-valor-distinto-2")
    chequear(otra.estado is not p.estado, "keys distintas: estados distintos")
    # misma key para las dos: la cuota agotada con una etiqueta frena a la otra
    m = Mundo()
    propia = m.materia("DESARROLLO", ["10-09.m4a"])
    amiga = m.materia("Historia Económica", ["11-09.m4a"])
    m.plan("chat", KEY_P, TPD)
    log, _ = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_P, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(m.quien("chat") == ["propia"], "una sola key física: el 429 diario frena a las dos materias", m.quien("chat"))
    # fallback: la propia pasa a ser la de ella y comparte su estado
    m = Mundo()
    amiga = m.materia("Historia Económica", ["11-09.m4a"])
    m.plan("whisper", KEY_A, error(401, "Invalid API Key"))
    log, t = correr(m, {"GROQ_API_KEY": KEY_P, "GROQ_API_KEY_AMIGA": KEY_A, "MATERIAS_AMIGA": "Historia Económica"})
    chequear(t.GROQ.para("Historia Económica") is t.GROQ.propia, "tras el rechazo, la materia de ella resuelve a la propia (mismo objeto)")


def prueba_c_sin_variables_nuevas():
    print("c) sin GROQ_API_KEY_AMIGA ni MATERIAS_AMIGA: igual que antes")
    ruta_vieja = os.environ.get("TRANSCRIBIR_ANTERIOR")
    if not ruta_vieja:
        print("  (omitida: definí TRANSCRIBIR_ANTERIOR con la ruta de la versión anterior para compararlas)")
        return

    def escenario(ruta, variante):
        m = Mundo()
        m.materia("DESARROLLO", ["01-09.m4a", "02-09.m4a"])
        m.materia("Macro", ["03-09.m4a"])
        env = {"GROQ_API_KEY": KEY_P}
        if variante == "sin_groq":
            env = {}
        if variante == "whisper_tpd":
            m.plan("whisper", KEY_P, None, WHISPER_TPD)
        if variante == "chat_tpd":
            m.plan("chat", KEY_P, None, TPD)
        if variante == "whisper_401":
            m.plan("whisper", KEY_P, error(401, "Invalid API Key"))
        correr(m, env, ruta=ruta, semilla=7)
        llamadas = [(c[0], c[1]) for c in m.llamadas]
        docs = sorted((f["name"], f["content"]) for f in m.drive.files.values() if not f["trashed"] and f["mimeType"] != FOLDER
                      and f["mimeType"] != "audio/mp4")
        return llamadas, docs

    for variante in ("normal", "sin_groq", "whisper_tpd", "chat_tpd", "whisper_401"):
        viejo, nuevo = escenario(ruta_vieja, variante), escenario(None, variante)
        chequear(viejo[0] == nuevo[0], f"{variante}: mismas llamadas a las APIs")
        chequear(viejo[1] == nuevo[1], f"{variante}: mismos Docs y contenidos en Drive")


def main():
    prueba_a1_dos_materias_cada_una_con_su_key()
    prueba_a2_key_de_ella_faltante()
    prueba_a3_key_de_ella_con_401()
    prueba_a4_cuota_agotada_en_una_sola_key()
    prueba_a5_alternancia()
    prueba_a6_fase0()
    prueba_a7_identidad_de_la_key()
    prueba_c_sin_variables_nuevas()
    print("\nRESULTADO:", "TODO OK" if not FALLAS else f"{len(FALLAS)} FALLA(S): {FALLAS}")
    sys.exit(1 if FALLAS else 0)


if __name__ == "__main__":
    main()
