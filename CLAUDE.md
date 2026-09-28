# Transcripciones UBA — guía para Claude Code

Sistema personal de Lautaro (Economía, FCE-UBA) que transcribe las clases que graba y deja todo listo para resumir. Hablale en español rioplatense.

## Flujo completo

```
Audio "dd-mm" en Drive (CLASES/<materia>/)
  -> transcribir.py (GitHub Actions, cada 30 min): transcribe y guarda Google Doc "dd-mm" en <materia>/Transcripciones
  -> Gemini Spark (tarea programada en la app de Gemini, NO en este repo): resume y guarda en Mi unidad/CLASES GRABDAS/<materia>/ "dd-mm - Resumen de clase" (carpeta SIN compartir, así Spark no pide confirmación)
  -> transcribir.py (fase 0): copia esos resúmenes a CLASES/<materia>/Resúmenes de clase
  -> Claude (tarea programada en el Proyecto de cada materia, fuera de este repo): suma los resúmenes al Proyecto
```

Este repo solo contiene la parte de Python. Spark y las tareas de Claude se configuran fuera.

## Archivos

- `transcribir.py`: todo el procesamiento.
- `.github/workflows/transcribir.yml`: cron `*/30 * * * *` (UTC), concurrency para no superponer corridas, instala ffmpeg.
- `requirements.txt`: google-api-python-client, google-auth, requests.
- `index.html`, `privacidad.html`: GitHub Pages, necesarias para que la app OAuth esté publicada. No borrar.

## Secretos (GitHub Actions)

`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (OAuth de la cuenta p.lautaro.gonzalez, scope drive completo), `GEMINI_API_KEY`, `CARPETA_CLASES_ID` (1iUMt_XeQoU5h2TJQgNp5ETqoEfqLbe7f), `CARPETA_RESUMENES_SPARK_ID` (ID de "CLASES GRABDAS", carpeta privada de Spark en la cuenta p.lautaro.gonzalez / paulogonzalito123, que son la misma; si falta, la fase 0 se saltea). Nunca imprimir ni commitear secretos.

## Cómo funciona transcribir.py

- Fase 0: copia resúmenes de Spark a la carpeta compartida (no usa IA).
- Transcripción: por cada audio sin Doc "dd-mm" en Transcripciones (orden aleatorio): baja el audio, lo parte con ffmpeg en tramos iguales de hasta 45 min (MP3 mono 16 kHz 48 kbps), transcribe cada tramo, guarda cada tramo en `Transcripciones/_partes` (así una corrida cortada se retoma), une y guarda como Google Doc.
- Resúmenes dentro del script: desactivados (`HACER_RESUMENES=false`). Los hace Spark.
- Límite de trabajo por corrida: 45 min. Las llamadas a Drive usan `num_retries`.

## Aprendizajes (no repetir errores)

- `gemini-3.5-transcribe`: máx. 98.304 tokens de entrada (~51 min; 32 tokens/s). El texto viene en `parts[].audioTranscription.text`. El recorte por tiempo (`video_metadata`) NO funciona con audio: hay que cortar con ffmpeg.
- La cuota gratuita diaria de pedidos de gemini-3.5-transcribe es el cuello de botella (~2-3 clases/día). Los 503 son saturación; cada reintento gasta cuota.
- Un 429 de Gemini lista varios límites: solo es "cuota diaria agotada" si TODOS son `PerDay`; si hay `PerMinute`, esperar `retryDelay`.
- La cuenta de servicio de Google no sirve (no puede crear archivos en carpetas de un usuario): por eso OAuth con refresh token.
- Probar cambios con Drive/Gemini simulados antes de commitear; después disparar el workflow (`gh workflow run`) y leer el log.

## Próximo cambio planeado: transcribir con Groq (Whisper)

Groq ofrece whisper-large-v3 gratis: ~28.800 s de audio por día (8 h), 7.200 s por hora, 20 pedidos/min, archivos de hasta 25 MB. Endpoint compatible con OpenAI: `POST https://api.groq.com/openai/v1/audio/transcriptions` (multipart: `file`, `model=whisper-large-v3`, `language=es`, `response_format=text`), header `Authorization: Bearer $GROQ_API_KEY`.

- Groq como transcriptor principal; Gemini (gemini-3.5-transcribe) como respaldo si Groq falla o agota cuota.
- Los tramos de 45 min en MP3 48 kbps pesan ~16 MB (entra en 25 MB). Verificar tamaño antes de subir.
- Whisper puede inventar o repetir frases en silencios largos: considerar `temperature=0` y detectar repeticiones.
- Evaluación planeada: comparar la transcripción de Groq con las de Gemini ya existentes (27-08, 07-09, 10-09 de DESARROLLO) antes de dejarlo como principal.

## Otros pendientes

- Crear el secreto `CARPETA_RESUMENES_SPARK_ID` (lo crea Lautaro).
- Regenerar el secreto del cliente OAuth (quedó expuesto en una captura) y actualizar `GOOGLE_CLIENT_SECRET`.
- GitHub desactiva workflows programados tras 60 días sin commits en repos públicos.
- Fase 0 empareja subcarpetas de "CLASES GRABDAS" con las materias de CLASES por nombre exacto. Mejora pendiente: normalizar tildes y mayúsculas al comparar (ej. "ECONOMETRIA II" vs "ECONOMETRÍA II"). Los resúmenes se COPIAN (no mover): Spark decide qué falta mirando su propia carpeta.
