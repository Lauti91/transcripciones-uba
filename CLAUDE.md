# Transcripciones UBA — guía para Claude Code

Sistema personal de Lautaro (Economía, FCE-UBA) que transcribe las clases que graba y deja todo listo para resumir. Hablale en español rioplatense.

## Flujo completo

```
Audio "dd-mm" en Drive (CLASES/<materia>/)
  -> transcribir.py (GitHub Actions, cada 30 min): transcribe (Groq Whisper), corrige con el glosario (Groq gpt-oss-120b) y guarda Google Doc "dd-mm" en <materia>/Transcripciones
  -> transcribir.py (fase 3): lo que quedó sin corregir por cuota se corrige después, reemplazando el Doc en el lugar
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
  - Glosario: primer archivo cuyo nombre empiece con "Glosario" en `CLASES/<materia>/Contexto` (Google Doc o texto; el de DESARROLLO es `Glosario.txt`, texto plano). Si no hay, corrige sin glosario.
  - Glosario por bloque (`seleccionar_glosario`): en cada pedido va lo fijo (todo lo anterior al primer "== ... ==" más las secciones cuyo título tenga docente/profesor/cronograma/calendario) y solo las entradas de las demás secciones que APAREZCAN o SE PAREZCAN a algo del bloque (palabra exacta, plural o similitud ≥ `UMBRAL_RELEVANCIA_GLOSARIO` 0,55 con palabras de 5+ letras; las de menos de 5 solo si aparecen tal cual). Se ordenan por parecido × rareza de la palabra (una palabra que está en muchas entradas pesa poco) y se cortan a `MAX_CARACTERES_GLOSARIO_VARIABLE` (4.200 ≈ 1.200 tokens). Un glosario sin secciones "== ==" se manda entero. El FILTRO de correcciones sigue usando el glosario COMPLETO. Ojo al escribir glosarios: una sección cuyo título diga "docente/profesor/cronograma/calendario" se manda entera siempre; no le pongas eso a una lista larga de conceptos. Con el glosario de Econometría (~18 KB ≈ 5.000 tokens) los bloques pasan de 200 a ~1.200 palabras y el glosario que se manda baja a ~800-1.500 tokens por bloque.
  - Tema: se busca la fecha "dd-mm" del audio en el cronograma del glosario, solo antes del primer ":" de cada línea (hay líneas como "24-08 y 27-08: tema: docente" y menciones cruzadas como "intercambiada con la del 14-09").
  - Bloques de ~1.200 palabras cortados en fin de oración (se achican solo si el glosario es muy largo). Pedido típico ≈ 6.000 tokens (entrada + salida): ~1 bloque por minuto con el límite de 8.000 TPM, y ~33 bloques (≈3 clases) por día con 200.000 TPD.
  - Límites: respeta `x-ratelimit-remaining-tokens` / `x-ratelimit-reset-tokens` antes de cada pedido; ante 429 por minuto espera `retry-after` (o el "try again in" del mensaje). 429 diario (TPD/RPD) => el resto de la corrida se guarda sin corregir. 3 intentos por bloque; si fallan 2 bloques seguidos, el resto queda sin corregir. La corrección nunca frena la transcripción.
  - Prompt: además de corregir solo con evidencia, prohíbe traducir, expandir abreviaturas o completar palabras cortadas, alargar nombres, cambiar números o su formato, cambiar apodos del glosario y corregir nombres propios cuyo destino no esté en el glosario. "Ante la duda, dejá el original."
  - Seguridad del bloque: si el `texto` del modelo mide <85% o >115% del original, o el JSON no sirve, se descarta el bloque entero (queda el original).
  - El texto final NO es el `texto` del modelo: se parte del bloque ORIGINAL y se le aplican solo las correcciones de la lista que pasan el filtro (una sola pasada, con límites de palabra). Así una corrección rechazada nunca queda escrita, y los cambios que el modelo no lista se pierden.
  - Filtro de cada corrección (`filtrar_correccion`), en orden; la primera regla que falla la descarta: solo formato o puntuación (incluye mayúsculas y guiones) · cambia números · agrega palabras · QUITA palabras (el fragmento corregido no puede tener menos palabras que el original: "Estados Unidos China → Estados Unidos" perdía una palabra) · apodo o forma de trato del glosario · completa una palabra cortada (el destino empieza con el original, o alguna palabra del original es el COMIENZO de la palabra corregida, evaluado palabra por palabra: "pa est → país está", "los pa → los países") · no aparece en el bloque · variante de otro término (si el original figura en el glosario como variante de un término, solo se acepta la corrección a ESE término: "Kabilis → Kaplan" cae porque Kabilis es variante de Kabeer) · nombre propio o sigla cuyo destino no está en el glosario · nombre propio poco parecido (similitud < `UMBRAL_SIMILITUD_NOMBRES`, 0,4, salvo que el original figure en el glosario como variante de ese término; en nombres de varias palabras se toma la peor palabra a palabra) · palabra común con similitud < `UMBRAL_SIMILITUD` (0,6 por defecto, configurable por env).
  - Completaciones de palabras cortadas: se descartan SIEMPRE, porque aceptarlas exigiría garantizar que la palabra elegida concuerda en género y número con las vecinas ("los pa" pide "países", no "país") y eso no se puede sin análisis gramatical. Criterio de Lautaro: mejor un falso descarte que una corrección mala (también se pierden "en tonces → entonces", "Camil → Camila", "RCT → RCTs"). Origen: 13-08 de DESARROLLO (29-09) aceptó "Estados Unidos China → Estados Unidos" y "pa est → país está".
  - Test de regresión en el repo: `tests/test_filtro_correcciones.py` (sin pytest ni red; `python tests/test_filtro_correcciones.py`). Fija las 7 correcciones reales de 13-08 (5 buenas aceptadas: Acemoglu, China, Malthus, Angus Maddison, Simon Kuznets; 2 malas descartadas) y variantes. El workflow lo corre ANTES de cada corrida (paso "Probar el filtro de correcciones"): si falla, la corrida no escribe nada. Si se cambia una regla a propósito, actualizar el test.
  - Similitud = 1 − Levenshtein / largo mayor, sin tildes ni mayúsculas. Con 0,5 NO caía "bota → aborto" (da 0,50; con difflib 0,60); con 0,6 caen bota y "dotes → dotaciones" (0,50) y la buena más justa es "decimios → deciles" (0,62).
  - Palabras comunes (no nombres ni siglas): se aceptan con similitud ≥ 0,6 como riesgo menor (decisión de Lautaro, 29-09); pueden colarse casos como "altano → alto" (probablemente era "cercano"). La regla estricta es solo para nombres propios y siglas.
  - Mayúsculas: los términos de la sección "CONCEPTOS Y TÉRMINOS" del glosario son sustantivos comunes: se escriben en minúscula a mitad de frase y con mayúscula a principio de oración (el modelo propone "Econometría" y queda "econometría"); también se corrige la forma con mayúscula inicial del original ("Gonometría" a principio de oración). Nombres, instituciones y siglas conservan sus mayúsculas.
  - Siglas en plural: se comparan sin la "s" final ("RCDs → RCTs" pasa porque RCT está en el glosario).
  - Casos reales del 24-09: "violera → Duflo" (era "pionera") cae por 0,14; "Mohamed Shams → Muhammad Yunus" cae por "Shams/Yunus" 0,20 (Gemini transcribió "Banerjee" ahí). "CEDE → CEPAL" (0,40) pasa si el modelo lo propone, porque CEPAL está en el glosario.
  - Nombre propio: sigla, mayúscula en medio de la frase o mayúscula que agrega el modelo. Una mayúscula solo por inicio de oración es ambigua: pasa si el destino está en el glosario y si no, se trata como palabra común.
  - Glosario para el filtro: comparación sin tildes, mayúsculas ni guiones Unicode (U+2010–U+2013 → "-", también al escribir). Apodos = formas entre paréntesis con aclaración después de ":" (ej. "Luciana Petrone (Luz, Lu: así la nombran en clase)"); los paréntesis sin ":" son variantes erróneas del término de esa línea (ej. "Naila Kabeer (Kabir, Javier, Kabilis)"); los títulos "== ... ==" no cuentan. Agregar una variante al glosario es la forma de autorizar una corrección poco parecida.
  - Salida: el Doc "dd-mm" es la versión corregida, con una sección final "Correcciones aplicadas" (lista original → corregido sin repetidos + conteo de bloques corregidos/descartados/sin corregir). La versión cruda queda en `_partes` como "dd-mm (sin corregir)" y NUNCA se modifica ni se borra.
  - Todo o nada: la corrección de una clase es completa (todos los bloques procesados; los que el modelo devolvió mal y se descartaron cuentan como procesados) o no se usa. Si la cuota, el tiempo o el corrector fallan a mitad, el Doc se guarda SIN corregir y SIN la sección (nunca "a medias") y queda para la fase 3.
- Fase 3, pendientes (`corregir_pendientes`, al final de cada corrida, después de las clases nuevas, que tienen prioridad): recorre los Docs de cada materia (solo los que tienen audio) de la fecha más vieja a la más nueva (por mes y día, entre todas las materias) y corrige los que estén pendientes:
  - Estado (`estado_correccion`): `sin` = no tiene la sección "Correcciones aplicadas"; `parcial` = la tiene pero con "sin corregir: N" N > 0 (Docs viejos que se cortaron por cuota; ej. 14-08 ECO INTER, 5 de 16 bloques); `completa` = la tiene con "sin corregir: 0". Es la señal de idempotencia: un Doc completo no se vuelve a tocar. `leer_texto` normaliza `\r\n` porque Docs exporta así.
  - Siempre desde el original de `_partes` ("dd-mm (sin corregir)"), nunca sobre un Doc ya modificado; un `parcial` se rehace ENTERO. Un `sin` sin original (las clases de DESARROLLO transcriptas el 28-09 antes de que existiera la corrección) se copia a `_partes` antes de corregirlo. Un `parcial` sin original no se toca. Un `sin` cuyo texto difiere del original de `_partes` (¿editado a mano?) no se toca.
  - Se reemplaza el contenido EN EL LUGAR con `drive.files().update(fileId, media_body=texto/plain)`: mismo archivo, mismo ID y mismo nombre; nunca se borra ni se recrea (no romper el vínculo con el resumen de Spark ni con la carpeta). Antes de escribir: el texto corregido no está vacío y mide entre 85 % y 115 % del original. Después de escribir se relee el Doc; si no coincide con lo escrito se restaura el contenido anterior y se cuenta como error (dos errores seguidos cortan la fase).
  - Todo o nada y corte limpio: si la cuota se acaba a mitad de una clase, ese Doc queda como estaba y la fase termina (el trabajo hecho en esa clase se pierde: posible mejora = checkpoint de bloques en `_partes`). Antes de empezar una clase se estima el tiempo (`MINUTOS_POR_BLOQUE_CORRECCION` 0,8 min por bloque) y, si no alcanza en lo que queda de los 45 min de la corrida, no se empieza.
  - `MAX_PENDIENTES=N` (input `max_pendientes` del workflow manual) limita a N clases por corrida; con 1 sirve de primera prueba real.
  - Modo `recorregir_audio` (con `prueba_materia`, input del workflow manual; env `RECORREGIR_AUDIO`): rehace una clase que YA figura como completa (ej. después de cambiar las reglas del filtro) desde su "(sin corregir)" de `_partes` y reemplaza el Doc en el lugar, con las mismas guardas, verificación y log. Lista en el log todas las correcciones aceptadas y descartadas y deja un CSV (artefacto `auditoria-correccion`). Si no hay original en `_partes`, no toca nada; si no se puede terminar (cuota), el Doc queda como estaba y la corrida FALLA. Corre en el grupo de concurrency normal (no en el de pruebas), serializado con las corridas programadas.
  - El log de cada reemplazo trae "VERIFICACIÓN 1-5" (mismo id/nombre/tipo, original en `_partes`, largos y %, sección y estado, sin duplicados) y "tokens reales de Groq en esta clase" (campo `usage` de la respuesta).
  - Costo: ~3-4 clases por día con el tope de 200.000 tokens diarios. El corrector no recuerda de un día para el otro qué clases quedaron: cada corrida las vuelve a detectar leyendo los Docs.
- Fase 0: empareja subcarpetas de Spark con materias ignorando tildes, mayúsculas y espacios de más.
- Límite conocido: `tema_de_clase` solo entiende fechas numéricas "dd-mm" en el cronograma del glosario; el de Econometría usa "19-ago", "03-sep y 07-sep", "10-sep en adelante", así que ahí el tema sale "no figura".
- Modos manuales (`workflow_dispatch`): input `prueba_materia` (env `PRUEBA_MATERIA`) más UNO de estos:
  - `prueba_groq_audio` (env `PRUEBA_GROQ_AUDIO`): transcribe ese audio solo con Groq, sin corrección, y guarda "<audio> (groq)".
  - `prueba_correccion_audio` (env `PRUEBA_CORRECCION_AUDIO`): toma la transcripción existente "<clase>" (sin su sección de correcciones, si la tiene), la corrige y guarda "<clase> (corregida)". El log lista cada corrección propuesta (OK / DESCARTADA + regla) y termina con un resumen por regla. Auditoría CSV (bloque, original, correccion, estado, regla) en `_partes/"<clase> (corregida) - auditoría.csv"` y como artefacto del workflow `auditoria-correccion`.
  - `recorregir_audio`: ver "Fase 3" (este SÍ reemplaza el Doc).
  - Las pruebas de Groq y de corrección no tocan el original, ni corren fase 0 u otras materias. Si la salida ya existe, no hace nada (borrarla para repetir). Corren en otro grupo de concurrency para que el cron no los cancele.
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
- Glosario de DESARROLLO (28-09): se sumaron los términos confirmados (Muhammad Yunus sin variantes, Bolsa Família, Econometría, Deciles, División sexual del trabajo, variantes de Duflo/Kabeer/J-PAL/RCT/empoderamiento/microcréditos). Pendientes de confirmar por Lautaro: Ravagnion, Muttiola, Morduch, CEDES. CEPAL ya estaba. El glosario actual lo subió el conector de Drive (dueño: lautigonzalez355, así que el conector lo puede reemplazar); el original de p.lautaro.gonzalez quedó renombrado "_viejo - Glosario.txt" (el conector no puede mandarlo a la papelera). El conector no puede editar contenido: para cambiar el glosario sube uno nuevo y manda el anterior a la papelera (primero crear, después borrar).
- Ojo: Spark podría resumir los Docs de prueba "(groq)" / "(corregida)" si mira toda la carpeta Transcripciones.

## Otros pendientes

- Regenerar el secreto del cliente OAuth (quedó expuesto en una captura) y actualizar `GOOGLE_CLIENT_SECRET`.
- GitHub desactiva workflows programados tras 60 días sin commits en repos públicos.
- Fase 0: los resúmenes se COPIAN (no mover): Spark decide qué falta mirando su propia carpeta.
