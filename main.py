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

from core import (
    BASE, Models, InputError, MAX_FILE_BYTES,
    evaluar, reply,
    evaluar_documento,
)
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
   
