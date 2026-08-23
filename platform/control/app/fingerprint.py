"""Structural fingerprint of a call site, so two repositories that broke the
same way can be matched without either one's source ever being stored.

The fingerprint is a hash of the *shape* of the call: identifiers are stripped
to placeholders, literals are bucketed by type. `client.chat(model="gpt-4", n=1)`
and `svc.chat(model="claude", n=9)` therefore land on the same fingerprint,
while `svc.chat("positional")` does not — the argument shape differs, and that
is exactly the distinction a migration transform turns on.

Python only. A structural notion of "the same call site" does not port to
another language without per-ecosystem work, and a fingerprint that is wrong
across repos is worse than no fingerprint: it produces confident false matches.
"""
import hashlib

import libcst as cst

VERSION = "fp1"     # bump if normalisation changes; old hashes stop matching new ones


def _literal_bucket(node: cst.CSTNode) -> str | None:
    """Literals collapse to their type. The value is customer data and is never
    part of the identity — only 'a string was passed here' is."""
    if isinstance(node, cst.SimpleString) or isinstance(node, cst.ConcatenatedString):
        return "STR"
    if isinstance(node, cst.FormattedString):
        return "FSTR"
    if isinstance(node, cst.Integer):
        return "INT"
    if isinstance(node, cst.Float) or isinstance(node, cst.Imaginary):
        return "FLOAT"
    if isinstance(node, cst.Name) and node.value in ("True", "False"):
        return "BOOL"
    if isinstance(node, cst.Name) and node.value == "None":
        return "NONE"
    if isinstance(node, (cst.List, cst.ListComp)):
        return "LIST"
    if isinstance(node, (cst.Dict, cst.DictComp)):
        return "DICT"
    if isinstance(node, (cst.Tuple,)):
        return "TUPLE"
    if isinstance(node, (cst.Set, cst.SetComp)):
        return "SET"
    return None


def _shape(node: cst.CSTNode) -> str:
    """A literal-and-identifier-free description of an expression."""
    bucket = _literal_bucket(node)
    if bucket:
        return bucket
    if isinstance(node, cst.Name):
        return "NAME"
    if isinstance(node, cst.Attribute):
        return f"ATTR({_shape(node.value)})"
    if isinstance(node, cst.Subscript):
        return f"SUB({_shape(node.value)})"
    if isinstance(node, cst.Call):
        return f"CALL({_shape(node.func)}/{len(node.args)})"
    if isinstance(node, cst.Await):
        return f"AWAIT({_shape(node.expression)})"
    if isinstance(node, (cst.BinaryOperation, cst.BooleanOperation)):
        return "BINOP"
    if isinstance(node, cst.Comparison):
        return "CMP"
    if isinstance(node, cst.Lambda):
        return "LAMBDA"
    return type(node).__name__.upper()


def dotted_name(node: cst.CSTNode) -> str | None:
    """`openai.ChatCompletion.create` from the func of a Call, if it is a plain
    dotted path. Returns None for anything computed — there is no stable
    cross-repo symbol identity for `handlers[name]()`."""
    parts: list[str] = []
    current = node
    while True:
        if isinstance(current, cst.Name):
            parts.append(current.value)
            break
        if isinstance(current, cst.Attribute):
            parts.append(current.attr.value)
            current = current.value
            continue
        return None
    return ".".join(reversed(parts))


def normalise_call(call: cst.Call) -> str:
    """The canonical, source-free description of one call site."""
    target = dotted_name(call.func)
    # Only the final attribute survives from the callee path: the receiver is
    # a local variable name in one repo and something else in the next.
    symbol = target.split(".")[-1] if target else _shape(call.func)

    positional: list[str] = []
    keyword: list[str] = []
    star = dstar = 0
    for arg in call.args:
        if arg.star == "*":
            star += 1
            continue
        if arg.star == "**":
            dstar += 1
            continue
        if arg.keyword is not None:
            # Keyword *names* are part of the vendor's API, not customer data,
            # and they are what signature changes actually break. They stay.
            keyword.append(f"{arg.keyword.value}={_shape(arg.value)}")
        else:
            positional.append(_shape(arg.value))

    return (f"{VERSION}|sym={symbol}|pos=[{','.join(positional)}]"
            f"|kw=[{','.join(sorted(keyword))}]|star={star}|dstar={dstar}")


def of_call(call: cst.Call) -> str:
    return hashlib.sha256(normalise_call(call).encode()).hexdigest()[:32]


def find_call(source: str, symbol_path: str) -> cst.Call | None:
    """The first call in `source` whose callee ends with `symbol_path`.

    Matching on the suffix is deliberate: the same vendor symbol is reached as
    `openai.ChatCompletion.create` in one repo and `ChatCompletion.create` in
    another, and both are the same break.
    """
    wanted = [p for p in symbol_path.split(".") if p]
    if not wanted:
        return None
    try:
        tree = cst.parse_module(source)
    except Exception:
        return None

    found: list[cst.Call] = []

    class Visitor(cst.CSTVisitor):
        def visit_Call(self, node: cst.Call) -> None:
            name = dotted_name(node.func)
            if not name:
                return
            parts = name.split(".")
            if parts[-len(wanted):] == wanted:
                found.append(node)

    tree.visit(Visitor())
    return found[0] if found else None


def of_source(source: str, symbol_path: str) -> str | None:
    """Fingerprint the call to `symbol_path` in `source`, or None if absent.

    The source is read and discarded; only the hash is ever returned, and the
    hash cannot be reversed into code.
    """
    call = find_call(source, symbol_path)
    return of_call(call) if call is not None else None
