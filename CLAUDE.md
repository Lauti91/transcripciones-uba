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

Opcionales para una segunda persona (ver "Segunda persona"): secret `GROQ_API_KEY_AMIGA` y variable de repo (NO secreta) `MATERIAS_AMIGA`. Sin ellas todo funciona como siempre.

## Cómo funciona transcribir.py

- Fase 0: copia resúmenes de Spark a la carpeta compartida (no usa IA).
- Transcripción: por cada audio sin Doc "dd-mm" en Transcripciones (orden aleatorio): baja el audio, lo parte con ffmpeg en tramos iguales de hasta 45 min (MP3 mono 16 kHz 48 kbps, ~15,5 MB), transcribe cada tramo con Groq (whisper-large-v3, `language=es`, `temperature=0`) y, si Groq falla o no tiene cuota, con gemini-3.5-transcribe, guarda cada tramo en `Transcripciones/_partes` (así una corrida cortada se retoma), une, corrige (ver abajo) y guarda como Google Doc.
- Groq: 429 diario (o con espera > 3 min, ej. límite por hora) => se usa Gemini el resto de la corrida; 429 por minuto => reintenta Groq; otro error en un tramo => ese tramo va a Gemini. Se colapsan frases idénticas repetidas 4+ veces seguidas (alucinación típica de Whisper en silencios). La pausa de 60 s entre tramos solo se hace tras un tramo de Gemini.
- Corrección (después de unir los tramos): Groq chat completions, modelo `openai/gpt-oss-120b` (constante `MODELO_CORRECCION`), `temperature=0`, `reasoning_effort=low`, JSON mode, con la key de Groq de la materia (`GROQ_API_KEY`, o `GROQ_API_KEY_AMIGA` en las materias de `MATERIAS_AMIGA`).
  - Glosario: primer archivo cuyo nombre empiece con "Glosario" en `CLASES/<materia>/Contexto` (Google Doc o texto; el de DESARROLLO es `Glosario.txt`, texto plano). Va completo en cada pedido. Si no hay, corrige sin glosario.
  - Tema: se busca la fecha "dd-mm" del audio en el cronograma del glosario, solo antes del primer ":" de cada línea (hay líneas como "24-08 y 27-08: tema: docente" y menciones cruzadas como "intercambiada con la del 14-09").
  - Bloques de ~1.200 palabras cortados en fin de oración (se achican solo si el glosario es muy largo). Pedido típico ≈ 6.000 tokens (entrada + salida): ~1 bloque por minuto con el límite de 8.000 TPM, y ~33 bloques (≈3 clases) por día con 200.000 TPD.
  - Límites: respeta `x-ratelimit-remaining-tokens` / `x-ratelimit-reset-tokens` antes de cada pedido; ante 429 por minuto espera `retry-after` (o el "try again in" del mensaje). 429 diario (TPD/RPD) => el resto de la corrida se guarda sin corregir, pero solo para las materias de ESA key (la otra sigue). 3 intentos por bloque; si fallan 2 bloques seguidos, el resto queda sin corregir. La corrección nunca frena la transcripción.
  - Prompt: además de corregir solo con evidencia, prohíbe traducir, expandir abreviaturas o completar palabras cortadas, alargar nombres, cambiar números o su formato, cambiar apodos del glosario y corregir nombres propios cuyo destino no esté en el glosario. "Ante la duda, dejá el original."
  - Seguridad del bloque: si el `texto` del modelo mide <85% o >115% del original, o el JSON no sirve, se descarta el bloque entero (queda el original).
  - El texto final NO es el `texto` del modelo: se parte del bloque ORIGINAL y se le aplican solo las correcciones de la lista que pasan el filtro (una sola pasada, con límites de palabra). Así una corrección rechazada nunca queda escrita, y los cambios que el modelo no lista se pierden.
  - Filtro de cada corrección (`filtrar_correccion`), en orden; la primera regla que falla la descarta: solo formato o puntuación (incluye mayúsculas y guiones) · cambia números · agrega palabras · apodo o forma de trato del glosario · completa una palabra cortada (el destino empieza con el original) · no aparece en el bloque · variante de otro término (si el original figura en el glosario como variante de un término, solo se acepta la corrección a ESE término: "Kabilis → Kaplan" cae porque Kabilis es variante de Kabeer) · nombre propio o sigla cuyo destino no está en el glosario · nombre propio poco parecido (similitud < `UMBRAL_SIMILITUD_NOMBRES`, 0,4, salvo que el original figure en el glosario como variante de ese término; en nombres de varias palabras se toma la peor palabra a palabra) · palabra común con similitud < `UMBRAL_SIMILITUD` (0,6 por defecto, configurable por env).
  - Similitud = 1 − Levenshtein / largo mayor, sin tildes ni mayúsculas. Con 0,5 NO caía "bota → aborto" (da 0,50; con difflib 0,60); con 0,6 caen bota y "dotes → dotaciones" (0,50) y la buena más justa es "decimios → deciles" (0,62).
  - Palabras comunes (no nombres ni siglas): se aceptan con similitud ≥ 0,6 como riesgo menor (decisión de Lautaro, 29-09); pueden colarse casos como "altano → alto" (probablemente era "cercano"). La regla estricta es solo para nombres propios y siglas.
  - Mayúsculas: los términos de la sección "CONCEPTOS Y TÉRMINOS" del glosario son sustantivos comunes: se escriben en minúscula a mitad de frase y con mayúscula a principio de oración (el modelo propone "Econometría" y queda "econometría"); también se corrige la forma con mayúscula inicial del original ("Gonometría" a principio de oración). Nombres, instituciones y siglas conservan sus mayúsculas.
  - Siglas en plural: se comparan sin la "s" final ("RCDs → RCTs" pasa porque RCT está en el glosario).
  - Casos reales del 24-09: "violera → Duflo" (era "pionera") cae por 0,14; "Mohamed Shams → Muhammad Yunus" cae por "Shams/Yunus" 0,20 (Gemini transcribió "Banerjee" ahí). "CEDE → CEPAL" (0,40) pasa si el modelo lo propone, porque CEPAL está en el glosario.
  - Nombre propio: sigla, mayúscula en medio de la frase o mayúscula que agrega el modelo. Una mayúscula solo por inicio de oración es ambigua: pasa si el destino está en el glosario y si no, se trata como palabra común.
  - Glosario para el filtro: comparación sin tildes, mayúsculas ni guiones Unicode (U+2010–U+2013 → "-", también al escribir). Apodos = formas entre paréntesis con aclaración después de ":" (ej. "Luciana Petrone (Luz, Lu: así la nombran en clase)"); los paréntesis sin ":" son variantes erróneas del término de esa línea (ej. "Naila Kabeer (Kabir, Javier, Kabilis)"); los títulos "== ... ==" no cuentan. Agregar una variante al glosario es la forma de autorizar una corrección poco parecida.
  - Salida: el Doc "dd-mm" es la versión corregida, con una sección final "Correcciones aplicadas" (lista original → corregido sin repetidos + conteo de bloques corregidos/descartados/sin corregir). La versión cruda queda en `_partes` como "dd-mm (sin corregir)".
  - Las clases que se guardaron sin corregir (por cuota o tiempo) NO se corrigen solas después; se pueden corregir con el modo prueba de corrección.
- Fase 0: empareja subcarpetas de Spark con materias ignorando tildes, mayúsculas y espacios de más. Si dos carpetas DISTINTAS normalizan al mismo nombre (en CLASES o del lado de Spark) no copia nada para esa materia y avisa en el log ("AVISO: ... no copio ...") en vez de adivinar.
- Modos prueba (`workflow_dispatch`): input `prueba_materia` (env `PRUEBA_MATERIA`) más UNO de estos:
  - `prueba_groq_audio` (env `PRUEBA_GROQ_AUDIO`): transcribe ese audio solo con Groq, sin corrección, y guarda "<audio> (groq)".
  - `prueba_correccion_audio` (env `PRUEBA_CORRECCION_AUDIO`): toma la transcripción existente "<clase>" (sin su sección de correcciones, si la tiene), la corrige y guarda "<clase> (corregida)". El log lista cada corrección propuesta (OK / DESCARTADA + regla) y termina con un resumen por regla. Auditoría CSV (bloque, original, correccion, estado, regla) en `_partes/"<clase> (corregida) - auditoría.csv"` y como artefacto del workflow `auditoria-correccion`.
  - Ninguno toca el original, ni corre fase 0 u otras materias. Si la salida ya existe, no hace nada (borrarla para repetir). Corren en otro grupo de concurrency para que el cron no los cancele.
- Orden de los audios: dentro de cada materia, aleatorio. Si hay pendientes de las dos personas (materias propias y de `MATERIAS_AMIGA`) se ALTERNAN uno y uno (`intercalar`; arranca una al azar para que ninguna lleve siempre el primer lugar). Si hay de una sola, no cambia nada. Antes de cada audio y de cada tramo se mira `tiempo_agotado()` (45 min); no hay estimación previa de cuánto tarda una clase.
- Resúmenes dentro del script: desactivados (`HACER_RESUMENES=false`). Los hace Spark.
- Límite de trabajo por corrida: 45 min. Las llamadas a Drive usan `num_retries`.

## Segunda persona (otra key de Groq)

Otra persona usa el mismo sistema con sus propias materias (carpetas normales dentro de CLASES, compartidas con ella como Editora; ella sube los audios "dd-mm ..."). Todo el flujo es igual; lo único distinto es QUÉ key de Groq se usa y cómo se lleva la cuota de cada key.

- Resolución (`ResolutorGroq.para(materia)`): si el nombre de la materia está en `MATERIAS_AMIGA` (nombres EXACTOS de carpeta separados por coma; se comparan sin tildes, mayúsculas ni espacios de más) y existe `GROQ_API_KEY_AMIGA`, se usa esa; si no, `GROQ_API_KEY`. Vale para TODO lo que usa Groq: transcripción (whisper) y corrección (gpt-oss-120b).
- Fallback a la key propia, siempre con aviso en el log ("fallback a key propia por ..."): falta `GROQ_API_KEY_AMIGA`, o Groq responde 401/403 con ella (se anota y esa key no se vuelve a probar en la corrida; el pedido que falló se repite con la propia). Cualquier otro error (429, 5xx, timeout) se maneja como siempre. Si tampoco hay key propia, esas materias siguen con Gemini.
- Etiquetas en el log: "propia" / "amiga" (ej. "tramo 1/1 OK con groq (key amiga)", "corrigiendo ... (key amiga; ...)"). Nunca se imprime el valor de una key: `log()` además tapa con `***` cualquier valor de key registrado.
- Cuota POR KEY: el estado (cuota de transcripción, cuota diaria de la corrección, presupuesto por minuto `x-ratelimit-*`, límite por minuto, rechazo 401/403) vive en `ESTADOS_GROQ`, indexado por la IDENTIDAD de la key (SHA-256 de su valor, solo en memoria), no por la etiqueta. Si dos etiquetas terminan en la misma key (fallback, o la misma key cargada dos veces) comparten estado. Si una key agota su cuota diaria solo se frena la corrección/transcripción de SUS materias; Gemini es una sola para todos (`ESTADO["sin_cuota_gemini"]`).
- Tests: `python tests/test_segunda_persona.py` (Drive, Groq y Gemini simulados, sin ffmpeg). Con `TRANSCRIBIR_ANTERIOR=<ruta a la versión anterior de transcribir.py>` también compara que, sin las variables nuevas, las llamadas a las APIs y los Docs resultantes sean idénticos.

### Cómo sumar a otra persona

1. Crear la carpeta de la materia dentro de CLASES y compartirla con la persona como Editora (los Docs, `_partes` y `Resúmenes de clase` se crean ahí con la cuenta del sistema).
2. Crear en la carpeta de Spark (la de `CARPETA_RESUMENES_SPARK_ID`) una subcarpeta con el MISMO nombre que la materia (sin importar tildes ni mayúsculas, pero un solo nombre: si hay dos que normalizan igual, la fase 0 no copia nada y avisa).
3. Cargar el secret `GROQ_API_KEY_AMIGA` con la key de Groq de la persona (Settings > Secrets and variables > Actions > Secrets).
4. Agregar el nombre de la carpeta de la materia a la variable de repo `MATERIAS_AMIGA` (pestaña Variables, no Secrets; varias materias separadas por coma).
5. Crear `Contexto/` dentro de la materia con un archivo `Glosario...` (Google Doc o texto), como en el resto de las materias.
6. Agregar la materia al prompt de la tarea de Spark para que genere los resúmenes en la subcarpeta del paso 2.
7. Ella sube los audios a la carpeta de la materia con nombre "dd-mm ..." y el sistema hace el resto. En el log de la corrida tiene que aparecer "Materias con key de la amiga (cargada): <materia>".

Ojo: este repo es público; no escribir acá nombres de personas, mails ni IDs de carpetas (los nombres de materia van en la variable, no en el código).

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
- Glosario de DESARROLLO (28-09): se sumaron los términos confirmados (Muhammad Yunus sin variantes, Bolsa Família, Econometría, Deciles, División sexual del trabajo, variantes de Duflo/Kabeer/J-PAL/RCT/empoderamiento/microcréditos). Pendientes de confirmar por Lautaro: Ravagnion, Muttiola, Morduch, CEDES. CEPAL ya estaba. El glosario actual lo subió el conector de Drive (dueño: lautigonzalez355, así que el conector lo puede reemplazar); el original de p.lautaro.gonzalez quedó renombrado "_viejo - Glosario.txt" (el conector no puede mandarlo a la papelera). El conector no puede editar contenido: para cambiar el glosario sube uno nuevo y manda el anterior a la papelera (primero crear, después borrar).
- Ojo: Spark podría resumir los Docs de prueba "(groq)" / "(corregida)" si mira toda la carpeta Transcripciones.

## Otros pendientes

- Regenerar el secreto del cliente OAuth (quedó expuesto en una captura) y actualizar `GOOGLE_CLIENT_SECRET`.
- GitHub desactiva workflows programados tras 60 días sin commits en repos públicos.
- Fase 0: los resúmenes se COPIAN (no mover): Spark decide qué falta mirando su propia carpeta.
