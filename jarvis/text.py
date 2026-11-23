import unicodedata


def normalize(text: str) -> str:
    """Minúsculas, sin tildes y con espacios simples, para comparar órdenes."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.split())


NEGATIVE_STARTS = ("no", "nop", "cancela", "cancelar", "mejor no", "detente", "para", "olvidalo",
                   "olvidate", "negativo", "nunca", "ni loco", "tampoco", "deja", "dejalo")
AFFIRMATIVE_WORDS = {"si", "claro", "adelante", "hazlo", "hagalo", "dale", "confirmo", "confirmado",
                     "acuerdo", "ok", "okay", "vale", "correcto", "afirmativo", "procede", "proceda",
                     "perfecto", "listo", "exacto", "obvio", "porfa", "favor", "supuesto"}


def confirmation(text: str | None) -> bool | None:
    """Interpreta una respuesta hablada: True (sí), False (no) o None si no queda claro."""
    if not text:
        return None
    words = normalize(text).replace(",", " ").replace(".", " ").replace("!", " ").replace("¡", " ").split()
    words = [word for word in words if word not in {"jarvis", "senor", "bueno", "pues", "eh", "mmm"}]
    if not words:
        return None
    phrase = " ".join(words)
    if any(phrase == start or phrase.startswith(start + " ") for start in NEGATIVE_STARTS):
        return False
    if AFFIRMATIVE_WORDS.intersection(words):
        return True
    return None
