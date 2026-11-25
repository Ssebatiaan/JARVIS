"""Cálculo exacto y seguro: solo números y operaciones, nunca código."""

import ast
import math
import operator
import re

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
MAX_VALUE = 1e15


class CalcError(ValueError):
    """La expresión no es un cálculo válido."""


def _value(node):
    if isinstance(node, ast.Expression):
        return _value(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        result = node.value
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        result = _UNARY[type(node.op)](_value(node.operand))
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _value(node.left), _value(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise CalcError("El resultado es demasiado grande.")
        result = _BINARY[type(node.op)](left, right)
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt"
          and len(node.args) == 1 and not node.keywords):
        result = math.sqrt(_value(node.args[0]))
    else:
        raise CalcError("Solo puedo calcular con números y operaciones.")
    if isinstance(result, complex) or abs(result) > MAX_VALUE:
        raise CalcError("El resultado es demasiado grande o no existe.")
    return result


def calculate(expression: str) -> str:
    text = expression.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", ".")
    # «20%» es un porcentaje; «20 % 3» es el resto de la división.
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%(?!\s*[\d(])", r"(\1/100)", text)
    try:
        value = _value(ast.parse(text.strip(), mode="eval"))
    except ZeroDivisionError:
        raise CalcError("No se puede dividir entre cero.") from None
    except CalcError:
        raise
    except (SyntaxError, ValueError, OverflowError, RecursionError, MemoryError):
        raise CalcError("No entendí el cálculo.") from None
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


_SPOKEN = re.compile(r"^(?:cuanto (?:es|son|da|seria)|calcula) (?P<rest>.+)$")
_WORDS = (("dividido entre", "/"), ("dividido por", "/"), ("multiplicado por", "*"), ("entre", "/"),
          ("por", "*"), ("mas", "+"), ("menos", "-"), ("x", "*"))


def spoken_expression(command: str) -> str | None:
    """«cuanto es 15 por 8» → «15*8». None si no es un cálculo simple con cifras."""
    match = _SPOKEN.match(command)
    if not match:
        return None
    text = re.sub(r"^(?:el )?(\d+(?:[.,]\d+)?) ?(?:%|por ciento) de (\d+(?:[.,]\d+)?)$", r"(\1/100)*\2", match["rest"])
    for word, symbol in _WORDS:
        text = re.sub(rf"(?<=[\d) ]){word}(?=[\d( ])", symbol, text)
    text = text.replace(" ", "")
    # «1.500» puede ser mil quinientos: con separador de miles decide la IA.
    if re.search(r"\d[.,]\d{3}(?!\d)", text):
        return None
    return text if re.fullmatch(r"[\d+\-*/().,]+", text) and re.search(r"\d\)?[+\-*/]", text) else None
