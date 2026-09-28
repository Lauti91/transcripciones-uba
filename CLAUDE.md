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

`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (OAuth de la cuenta p.lautaro.gonzalez, scope drive completo), `GEMINI_API_KEY`, `GROQ_API_KEY` (si falta, se transcribe solo con Gemini), `CARPETA_CLASES_ID` (1iUMt_XeQoU5h2TJQgNp5ETqoEfqLbe7f), `CARPETA_RESUMENES_SPARK_ID` (ID de "CLASES GRABDAS", carpeta privada de Spark en la cuenta p.lautaro.gonzalez / paulogonzalito123, que son la misma; si falta, la fase 0 se saltea). Nunca imprimir ni commitear secretos.

## Cómo funciona transcribir.py

- Fase 0: copia resúmenes de Spark a la carpeta compartida (no usa IA).
- Transcripción: por cada audio sin Doc "dd-mm" en Transcripciones (orden aleatorio): baja el audio, lo parte con ffmpeg en tramos iguales de hasta 45 min (MP3 mono 16 kHz 48 kbps, ~15,5 MB), transcribe cada tramo con Groq (whisper-large-v3, `language=es`, `temperature=0`) y, si Groq falla o no tiene cuota, con gemini-3.5-transcribe, guarda cada tramo en `Transcripciones/_partes` (así una corrida cortada se retoma), une y guarda como Google Doc.
- Groq: 429 diario (o con espera > 3 min, ej. límite por hora) => se usa Gemini el resto de la corrida; 429 por minuto => reintenta Groq; otro error en un tramo => ese tramo va a Gemini. Se colapsan frases idénticas repetidas 4+ veces seguidas (alucinación típica de Whisper en silencios). La pausa de 60 s entre tramos solo se hace tras un tramo de Gemini.
- Fase 0: empareja subcarpetas de Spark con materias ignorando tildes, mayúsculas y espacios de más.
- Modo prueba Groq: `workflow_dispatch` con inputs `prueba_groq_materia` y `prueba_groq_audio` (env `PRUEBA_GROQ_MATERIA` / `PRUEBA_GROQ_AUDIO`). Transcribe solo ese audio, solo con Groq, y guarda "<audio> (groq)" en Transcripciones sin tocar nada más (no corre fase 0 ni otras materias). Si ya existe, no hace nada. Corre en otro grupo de concurrency para que el cron no lo cancele.
- Resúmenes dentro del script: desactivados (`HACER_RESUMENES=false`). Los hace Spark.
- Límite de trabajo por corrida: 45 min. Las llamadas a Drive usan `num_retries`.

## Aprendizajes (no repetir errores)

- `gemini-3.5-transcribe`: máx. 98.304 tokens de entrada (~51 min; 32 tokens/s). El texto viene en `parts[].audioTranscription.text`. El recorte por tiempo (`video_metadata`) NO funciona con audio: hay que cortar con ffmpeg.
- La cuota gratuita diaria de pedidos de gemini-3.5-transcribe es el cuello de botella (~2-3 clases/día). Los 503 son saturación; cada reintento gasta cuota.
- Un 429 de Gemini lista varios límites: solo es "cuota diaria agotada" si TODOS son `PerDay`; si hay `PerMinute`, esperar `retryDelay`.
- La cuenta de servicio de Google no sirve (no puede crear archivos en carpetas de un usuario): por eso OAuth con refresh token.
- Probar cambios con Drive/Gemini simulados antes de commitear; después disparar el workflow (`gh workflow run`) y leer el log.

## Groq (Whisper): transcriptor principal

Groq ofrece whisper-large-v3 gratis: ~28.800 s de audio por día (8 h), 7.200 s por hora, 20 pedidos/min, archivos de hasta 25 MB. Endpoint compatible con OpenAI: `POST https://api.groq.com/openai/v1/audio/transcriptions`.

- Pendiente: comparar "27-08 (groq)" contra "27-08" (Gemini) de DESARROLLO, y quizá 07-09 y 10-09, antes de darlo por bueno.
- Ojo: Spark podría resumir los Docs "(groq)" de prueba si mira toda la carpeta Transcripciones.

## Otros pendientes

- Regenerar el secreto del cliente OAuth (quedó expuesto en una captura) y actualizar `GOOGLE_CLIENT_SECRET`.
- GitHub desactiva workflows programados tras 60 días sin commits en repos públicos.
- Fase 0: los resúmenes se COPIAN (no mover): Spark decide qué falta mirando su propia carpeta.
