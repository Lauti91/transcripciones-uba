"""Regresión del filtro de correcciones (transcribir.filtrar_correccion).

Fija los casos reales de 13-08 de DESARROLLO (28-29/09/2026): el modelo propuso 7 correcciones que
pasaron el filtro; 5 eran buenas y 2 malas. Las dos malas no pueden volver a pasar y las cinco
buenas tienen que seguir pasando. Se corre con:  python tests/test_filtro_correcciones.py
(sin pytest, sin red, sin Drive; también sirve como test de pytest).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcribir as t  # noqa: E402

GLOSARIO = """GLOSARIO - DESARROLLO (fixture del test)

== DOCENTES ==
Andrés López (profesor titular)

== AUTORES ==
Daron Acemoglu
Thomas Malthus
Angus Maddison
Simon Kuznets

== CONCEPTOS Y TÉRMINOS ==
Desacoplamiento estratégico (Estados Unidos-China)
Estado y mercado
Empoderamiento / empoderadas / empoderado (empoladas)
Econometría (gonometría)
"""

# Frases textuales de la transcripción real de 13-08.
SEMOGLU = "de varias clases. Semoglu, premio Nobel de economía. Terminar con la Semoglu, en una"
CHINI = "como ven el Chini bajó un poco, este es el Chini en los datos"
MALTOS = "¿Saben quién es? Maltos. ¿No? Maltos. Maltos. Yo no, la verdad"
MADISON = "0? pregúntenle a August Madison, les va a decir cuánto era"
KUZNETS = "algo inventado por Simón Kuznets que después se usó"
EEUU_CHINA = "entre China y Estados Unidos China, la burbuja. Dicen que Estados Unidos China es un"
PA_EST = "para decir si los pa est consumiendo no tienen"

# (original, corregido, bloque, resultado esperado): None = se acepta; texto = regla que la descarta
CASOS = [
    # Las cinco buenas de 13-08: siguen aceptadas.
    ("Semoglu", "Acemoglu", SEMOGLU, None),
    ("Chini", "China", CHINI, None),
    ("Maltos", "Malthus", MALTOS, None),
    ("August Madison", "Angus Maddison", MADISON, None),
    ("Simón Kuznets", "Simon Kuznets", KUZNETS, None),
    # Las dos malas de 13-08: descartadas.
    ("Estados Unidos China", "Estados Unidos", EEUU_CHINA, "quita palabras"),
    ("pa est", "país está", PA_EST, "completa una palabra cortada"),
    # Variantes de las mismas reglas.
    ("los pa", "los países", "los pa consumiendo", "completa una palabra cortada"),
    ("pol", "política", "la pol pública", "completa una palabra cortada"),
    ("Camil", "Camila", "como dice Camil en clase", "completa una palabra cortada"),
    ("Estado y", "Estado", "el Estado y el mercado", "quita palabras"),
    ("en tonces", "entonces", "y en tonces vemos", "solo formato"),        # falso descarte aceptable
    ("David", "David Weil", "lo que dice David", "agrega palabras"),
    # Controles: correcciones buenas que no se tocan.
    ("gonometría", "econometría", "la gonometría del curso", None),
    ("empoladas", "empoderadas", "mujeres empoladas y libres", None),
]


def evaluar():
    info = t.preparar_glosario(GLOSARIO)
    resultados = []
    for original, corregido, bloque, esperado in CASOS:
        regla = t.filtrar_correccion(original, corregido, bloque, info)
        ok = regla is None if esperado is None else (regla is not None and regla.startswith(esperado))
        resultados.append((ok, original, corregido, esperado, regla))
    return resultados


def test_filtro_correcciones():
    malos = [r for r in evaluar() if not r[0]]
    assert not malos, f"casos que cambiaron de resultado: {malos}"


if __name__ == "__main__":
    fallas = 0
    for ok, original, corregido, esperado, regla in evaluar():
        fallas += not ok
        detalle = "" if ok else f"   (esperado: {esperado or 'aceptada'})"
        print(f"{'OK   ' if ok else 'FALLA'} {original!r} -> {corregido!r}: {regla or 'aceptada'}{detalle}")
    print("RESULTADO:", "TODO OK" if not fallas else f"{fallas} FALLA(S)")
    sys.exit(1 if fallas else 0)
