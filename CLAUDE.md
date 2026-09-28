# Transcripciones UBA — guía para Claude Code

Sistema personal de Lautaro (Economía, FCE-UBA) que transcribe las clases que graba y deja todo listo para resumir. Hablale en español rioplatense.

## Flujo completo

```
Audio "dd-mm" en Drive (CLASES/<materia>/)
  -> transcribir.py (GitHub Actions, cada 30 min): transcribe (Groq Whisper), corrige con el glosario (Groq gpt-oss-120b) y guarda Google Doc "dd-mm" en <materia>/Transcripciones
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
- Transcripción: por cada audio sin Doc "dd-mm" en Transcripciones (orden aleatorio): baja el audio, lo parte con ffmpeg en tramos iguales de hasta 45 min (MP3 mono 16 kHz 48 kbps, ~15,5 MB), transcribe cada tramo con Groq (whisper-large-v3, `language=es`, `temperature=0`) y, si Groq falla o no tiene cuota, con gemini-3.5-transcribe, guarda cada tramo en `Transcripciones/_partes` (así una corrida cortada se retoma), une, corrige (ver abajo) y guarda como Google Doc.
- Groq: 429 diario (o con espera > 3 min, ej. límite por hora) => se usa Gemini el resto de la corrida; 429 por minuto => reintenta Groq; otro error en un tramo => ese tramo va a Gemini. Se colapsan frases idénticas repetidas 4+ veces seguidas (alucinación típica de Whisper en silencios). La pausa de 60 s entre tramos solo se hace tras un tramo de Gemini.
- Corrección (después de unir los tramos): Groq chat completions, modelo `openai/gpt-oss-120b` (constante `MODELO_CORRECCION`), `temperature=0`, `reasoning_effort=low`, JSON mode, misma `GROQ_API_KEY`.
  - Glosario: primer archivo cuyo nombre empiece con "Glosario" en `CLASES/<materia>/Contexto` (Google Doc o texto; el de DESARROLLO es `Glosario.txt`, texto plano). Va completo en cada pedido. Si no hay, corrige sin glosario.
  - Tema: se busca la fecha "dd-mm" del audio en el cronograma del glosario, solo antes del primer ":" de cada línea (hay líneas como "24-08 y 27-08: tema: docente" y menciones cruzadas como "intercambiada con la del 14-09").
  - Bloques de ~1.200 palabras cortados en fin de oración (se achican solo si el glosario es muy largo). Pedido típico ≈ 6.000 tokens (entrada + salida): ~1 bloque por minuto con el límite de 8.000 TPM, y ~33 bloques (≈3 clases) por día con 200.000 TPD.
  - Límites: respeta `x-ratelimit-remaining-tokens` / `x-ratelimit-reset-tokens` antes de cada pedido; ante 429 por minuto espera `retry-after` (o el "try again in" del mensaje). 429 diario (TPD/RPD) => el resto de la corrida se guarda sin corregir. 3 intentos por bloque; si fallan 2 bloques seguidos, el resto queda sin corregir. La corrección nunca frena la transcripción.
  - Seguridad: si el bloque corregido mide <85% o >115% del original, o el JSON no sirve, se descarta y queda el original (sus correcciones no se listan).
  - Salida: el Doc "dd-mm" es la versión corregida, con una sección final "Correcciones aplicadas" (lista original → corregido sin repetidos + conteo de bloques corregidos/descartados/sin corregir). La versión cruda queda en `_partes` como "dd-mm (sin corregir)".
  - Las clases que se guardaron sin corregir (por cuota o tiempo) NO se corrigen solas después; se pueden corregir con el modo prueba de corrección.
- Fase 0: empareja subcarpetas de Spark con materias ignorando tildes, mayúsculas y espacios de más.
- Modos prueba (`workflow_dispatch`): input `prueba_materia` (env `PRUEBA_MATERIA`) más UNO de estos:
  - `prueba_groq_audio` (env `PRUEBA_GROQ_AUDIO`): transcribe ese audio solo con Groq, sin corrección, y guarda "<audio> (groq)".
  - `prueba_correccion_audio` (env `PRUEBA_CORRECCION_AUDIO`): toma la transcripción existente "<clase>" (sin su sección de correcciones, si la tiene), la corrige y guarda "<clase> (corregida)"; el log lista todas las correcciones.
  - Ninguno toca el original, ni corre fase 0 u otras materias. Si la salida ya existe, no hace nada (borrarla para repetir). Corren en otro grupo de concurrency para que el cron no los cancele.
- Resúmenes dentro del script: desactivados (`HACER_RESUMENES=false`). Los hace Spark.
- Límite de trabajo por corrida: 45 min. Las llamadas a Drive usan `num_retries`.

## Aprendizajes (no repetir errores)

- `gemini-3.5-transcribe`: máx. 98.304 tokens de entrada (~51 min; 32 tokens/s). El texto viene en `parts[].audioTranscription.text`. El recorte por tiempo (`video_metadata`) NO funciona con audio: hay que cortar con ffmpeg.
- La cuota gratuita diaria de pedidos de gemini-3.5-transcribe es el cuello de botella (~2-3 clases/día). Los 503 son saturación; cada reintento gasta cuota.
- Un 429 de Gemini lista varios límites: solo es "cuota diaria agotada" si TODOS son `PerDay`; si hay `PerMinute`, esperar `retryDelay`.
- La cuenta de servicio de Google no sirve (no puede crear archivos en carpetas de un usuario): por eso OAuth con refresh token.
- Groq chat (free tier) para la corrección: 8.000 tokens/min y 200.000/día; gpt-oss-120b es un modelo con razonamiento y esos tokens también cuentan (por eso `reasoning_effort=low`).
- Probar cambios con Drive/Gemini simulados antes de commitear; después disparar el workflow (`gh workflow run`) y leer el log.

## Groq (Whisper): transcriptor principal

Endpoint compatible con OpenAI: `POST https://api.groq.com/openai/v1/audio/transcriptions`, archivos de hasta 25 MB.

- Límites de whisper-large-v3 NO confirmados: la documentación hablaba de ~8 h de audio por día y 2 h por hora, pero el 28-09 se transcribieron ~12,7 h en 11 min sin ningún 429. El código no depende de esos números: reacciona a los 429.
- Evaluación (28-09, hecha por Lautaro con 27-08 de DESARROLLO): Groq captura todo el contenido pero le erra a nombres y términos (Kabir/Javier → Kabeer, Duflot → Duflo; Banerjee no aparece) y deja algunos pasajes ininteligibles. Gemini es más limpio, pero su cuota diaria y los 503 son el problema. Decisión: Groq principal + pasada de corrección con glosario.
- Ojo: Spark podría resumir los Docs de prueba "(groq)" / "(corregida)" si mira toda la carpeta Transcripciones.

## Otros pendientes

- Regenerar el secreto del cliente OAuth (quedó expuesto en una captura) y actualizar `GOOGLE_CLIENT_SECRET`.
- GitHub desactiva workflows programados tras 60 días sin commits en repos públicos.
- Fase 0: los resúmenes se COPIAN (no mover): Spark decide qué falta mirando su propia carpeta.
