import unicodedata


def normalize(text: str) -> str:
    """Minúsculas, sin tildes y con espacios simples, para comparar órdenes."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.split())
