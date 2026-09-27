# core_normalizador.py
import re
import ahocorasick
from typing import Iterable, Iterator, List, Dict, Tuple, Optional

# ============================================================
# ALIASES (mismos que el frontend)
# ============================================================
FIELD_ALIASES = {
    "correo": "correo", "email": "correo", "e-mail": "correo", "mail": "correo",
    "usuario": "usuario", "user": "usuario", "user_name": "usuario", "username": "usuario",
    "clave": "clave", "pass": "clave", "password": "clave", "pwd": "clave",
    "contrasena": "clave", "contraseña": "clave", "contra": "clave",
    "pin": "pin", "pincode": "pin",
    "cualquiera": "skip", "any": "skip", "...": "skip", "*": "skip",
}

CAPTURES = {
    "correo": r"([a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)",
    "usuario": r"([a-zA-Z0-9._\-]{1,64})",
    "clave": r"(\S+)",
    "pin": r"(\d{2,8})",
    "skip": r"([\s\S]*?)",
}

EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$"
)

# ============================================================
# CONVERSIÓN DE PLANTILLA A REGEX
# ============================================================
def _escape_regex(s: str) -> str:
    return re.escape(s)


def _literal_to_pattern(s: str) -> str:
    return re.sub(r"\s+", r"\\s*", _escape_regex(s))


_PH_RE = re.compile(r"<<\s*([^\s<>]+)\s*>>")


def template_to_regex(plantilla: str) -> Tuple[Optional[re.Pattern], List[str], Optional[str]]:
    raw = plantilla.replace("\\n", "\n").replace("\\t", "\t")
    fields: List[str] = []
    parts: List[str] = []
    last = 0
    for m in _PH_RE.finditer(raw):
        parts.append(_literal_to_pattern(raw[last:m.start()]))
        key = m.group(1).lower()
        canon = FIELD_ALIASES.get(key)
        if canon:
            fields.append(canon)
            parts.append(CAPTURES[canon])
        else:
            parts.append(_literal_to_pattern(m.group(0)))
        last = m.end()
    parts.append(_literal_to_pattern(raw[last:]))

    if not fields or "correo" not in fields:
        return None, fields, "Falta la variable <<correo>>"

    source = "".join(parts)
    try:
        return re.compile(source, re.IGNORECASE), fields, None
    except re.error as e:
        return None, fields, f"Error regex: {e}"


# ============================================================
# NORMALIZADOR CON AHOCORASICK + REGEX
# ============================================================
class NormalizadorPro:
    """
    Procesa texto grande en streaming usando:
      - Aho-Corasick para encontrar el prefijo literal de cada plantilla.
      - Regex precompiladas para extraer los campos.
      - Deduplicación por correo en un set en memoria.
    """

    def __init__(self, plantillas: List[Dict], dedup: bool = True):
        self.plantillas_activas = []
        self.automata = ahocorasick.Automaton()
        self.dedup = dedup

        for p in plantillas:
            if not p.get("activo", True):
                continue
            regex, fields, err = template_to_regex(p["plantilla"])
            if regex is None:
                continue
            raw = p["plantilla"].replace("\\n", "\n").replace("\\t", "\t")
            first_ph = _PH_RE.search(raw)
            prefijo = raw[:first_ph.start()] if first_ph else raw
            prefijo_norm = re.sub(r"\s+", " ", prefijo).strip()
            if not prefijo_norm:
                prefijo_norm = f"__FMT_{p.get('id', len(self.plantillas_activas))}__"

            info = {
                "id": p.get("id", prefijo_norm),
                "nombre": p.get("nombre", "sin_nombre"),
                "regex": regex,
                "fields": fields,
                "prefijo": prefijo_norm,
            }
            self.plantillas_activas.append(info)
            self.automata.add_word(prefijo_norm, info["id"])

        self.automata.make_automaton()
        self.por_id = {p["id"]: p for p in self.plantillas_activas}
        self.vistos: set = set()

    def _extraer_bloque(self, bloque: str) -> Iterator[Dict]:
        for end_idx, fmt_id in self.automata.iter(bloque):
            info = self.por_id.get(fmt_id)
            if info is None:
                continue
            start = bloque.rfind(info["prefijo"], 0, end_idx + 1)
            if start == -1:
                continue
            m = info["regex"].search(bloque, start)
            if not m:
                continue

            rec = {
                "correo": "", "usuario": "", "clave": "", "pin": "",
                "formato": info["nombre"], "estado": "OK", "motivo": "",
            }
            for i, field in enumerate(info["fields"]):
                val = (m.group(i + 1) or "").strip()
                if not val:
                    continue
                if field == "correo":
                    rec["correo"] = val
                elif field == "usuario":
                    rec["usuario"] = val
                elif field == "clave":
                    rec["clave"] = val
                elif field == "pin":
                    rec["pin"] = val

            if not rec["correo"] or not EMAIL_RE.match(rec["correo"]):
                continue
            if not rec["usuario"]:
                rec["usuario"] = rec["correo"].split("@")[0]

            yield rec

    def procesar_stream(self, stream: Iterable[str]) -> Iterator[Dict]:
        buffer = ""
        for chunk in stream:
            buffer += chunk
            while True:
                corte = buffer.find("\n\n")
                if corte == -1 and len(buffer) < 500_000:
                    break
                if corte == -1:
                    corte = len(buffer)
                bloque = buffer[:corte]
                buffer = buffer[corte + 2:]
                for rec in self._extraer_bloque(bloque):
                    if self.dedup:
                        key = rec["correo"].lower()
                        if key in self.vistos:
                            continue
                        self.vistos.add(key)
                    yield rec
        if buffer:
            for rec in self._extraer_bloque(buffer):
                if self.dedup:
                    key = rec["correo"].lower()
                    if key in self.vistos:
                        continue
                    self.vistos.add(key)
                yield rec
