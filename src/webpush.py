"""Web Push — VAPID (RFC 8292) and aes128gcm payloads (RFC 8291).

Written against `cryptography`, which this project already depends on, rather
than pulling in a push library: the whole of it is an ECDH, an HKDF and an
AES-GCM seal, and a dependency that wraps those is mostly indirection.

Why this rather than a push service like ntfy: the browser is the client. A
device that has opened Odysseus can receive notifications with no extra app
installed, and tapping one opens the page it came from. The cost is that the
endpoint belongs to the browser vendor's push service, so a subscription is
per-browser-per-device and disappears if the user clears site data.

The server never learns anything the push service can read: the payload is
encrypted to the subscription's own P-256 key, and the VAPID token only proves
the push came from this server.
"""

import base64
import json
import logging
import os
import struct
import tempfile
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

KEYS_FILE = os.path.join(DATA_DIR, "webpush_keys.json")
SUBS_FILE = os.path.join(DATA_DIR, "webpush_subscriptions.json")

# RFC 8292: tokens must not be valid for more than 24h. Stay well inside it.
VAPID_TTL_S = 12 * 60 * 60
# RFC 8291 fixes the record size used here; 4096 is what browsers accept.
RECORD_SIZE = 4096


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _atomic_write(path: str, obj) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".wp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── VAPID identity ───────────────────────────────────────────────────────────

def _load_keys() -> Optional[Dict]:
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_or_create_keys() -> Dict:
    """The server's VAPID keypair, generated once and reused.

    Regenerating would invalidate every existing subscription, since browsers
    tie a subscription to the key that created it.
    """
    keys = _load_keys()
    if keys:
        return keys
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    keys = {
        "private_pem": priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii"),
        "public_key": b64u(pub),
        "created": time.time(),
    }
    _atomic_write(KEYS_FILE, keys)
    os.chmod(KEYS_FILE, 0o600)
    logger.info("[webpush] generated a new VAPID keypair")
    return keys


def public_key() -> str:
    return get_or_create_keys()["public_key"]


def _private_key() -> ec.EllipticCurvePrivateKey:
    pem = get_or_create_keys()["private_pem"].encode("ascii")
    return serialization.load_pem_private_key(pem, password=None)


def _vapid_header(endpoint: str, subject: str) -> Dict[str, str]:
    """Signed JWT proving this server sent the push (RFC 8292)."""
    parsed = urlparse(endpoint)
    claims = {
        "aud": f"{parsed.scheme}://{parsed.netloc}",
        "exp": int(time.time()) + VAPID_TTL_S,
        "sub": subject,
    }
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"},
                             separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")

    der = _private_key().sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # JWS wants the raw r||s pair, not the DER structure ECDSA produces.
    r, s = asym_utils.decode_dss_signature(der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = f"{header}.{body}.{b64u(raw_sig)}"
    return {
        "Authorization": f"vapid t={token}, k={public_key()}",
    }


# ── Payload encryption (RFC 8291) ────────────────────────────────────────────

def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """Seal `payload` to a subscription's key, in aes128gcm content coding."""
    client_pub_bytes = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    client_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), client_pub_bytes)

    # Ephemeral per message: reusing it would let two messages be correlated.
    server_priv = ec.generate_private_key(ec.SECP256R1())
    server_pub_bytes = server_priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    shared = server_priv.exchange(ec.ECDH(), client_pub)

    salt = os.urandom(16)
    # The spec's two-stage derivation: the auth secret first mixes the two
    # public keys in, then the salt derives the actual key and nonce.
    prk_info = b"WebPush: info\x00" + client_pub_bytes + server_pub_bytes
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=prk_info).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)

    # A single record, so the delimiter is 0x02 ("last record").
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!I", RECORD_SIZE) + \
        bytes([len(server_pub_bytes)]) + server_pub_bytes
    return header + ciphertext


# ── Subscriptions ────────────────────────────────────────────────────────────

def load_subscriptions() -> List[Dict]:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_subscription(sub: Dict, *, device: str = "", owner: str = "") -> Dict:
    """Store a PushSubscription. The endpoint is the identity — re-subscribing
    the same browser yields the same endpoint, so this upserts."""
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("subscription needs endpoint and keys.p256dh/auth")

    rec = {
        "endpoint": endpoint,
        "p256dh": keys["p256dh"],
        "auth": keys["auth"],
        "device": device or "",
        "owner": owner or "",
        "created": time.time(),
    }
    subs = [s for s in load_subscriptions() if s.get("endpoint") != endpoint]
    subs.append(rec)
    _atomic_write(SUBS_FILE, subs)
    return rec


def remove_subscription(endpoint: str) -> bool:
    subs = load_subscriptions()
    keep = [s for s in subs if s.get("endpoint") != endpoint]
    if len(keep) == len(subs):
        return False
    _atomic_write(SUBS_FILE, keep)
    return True


def _subject() -> str:
    """VAPID `sub`: a contact for whoever runs this server."""
    return os.environ.get("VAPID_SUBJECT") or "mailto:admin@localhost"


async def send(title: str, body: str, *, device: str = "", url: str = "",
               tag: str = "odysseus", command: str = "",
               command_arg: str = "") -> Dict:
    """Push to every matching subscription. Returns a per-endpoint summary."""
    import httpx

    subs = load_subscriptions()
    if device:
        want = device.strip().lower()
        subs = [s for s in subs if want in (s.get("device") or "").lower()]
    if not subs:
        return {"sent": 0, "failed": 0, "detail": "no matching subscriptions"}

    msg = {"title": title, "body": body, "url": url, "tag": tag}
    if command:
        # An on-device automation matches on these; the service worker shows
        # the notification either way.
        msg["command"] = command
        if command_arg:
            msg["arg"] = command_arg
    payload = json.dumps(msg).encode()
    sent = failed = 0
    errors: List[str] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for s in subs:
            try:
                encrypted = _encrypt(payload, s["p256dh"], s["auth"])
                headers = _vapid_header(s["endpoint"], _subject())
                headers.update({
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": "86400",
                    "Urgency": "normal",
                })
                r = await client.post(s["endpoint"], content=encrypted, headers=headers)
                if r.status_code in (200, 201, 202, 204):
                    sent += 1
                elif r.status_code in (404, 410):
                    # The browser threw the subscription away; stop trying it.
                    remove_subscription(s["endpoint"])
                    errors.append(f"{r.status_code} gone — subscription dropped")
                    failed += 1
                else:
                    failed += 1
                    errors.append(f"HTTP {r.status_code}: {r.text[:120]}")
            except Exception as e:
                failed += 1
                errors.append(f"{type(e).__name__}: {e}")
    return {"sent": sent, "failed": failed, "errors": errors[:5]}
