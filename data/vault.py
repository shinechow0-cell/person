"""密码保险库 — AES-256-GCM 加密存储
vault.json.enc: 二进制加密文件
主密码 → PBKDF2 派生密钥 → 解密到内存 → 操作 → 加密写回
"""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

STORE = Path(__file__).resolve().parent / "vault.json.enc"

CATEGORIES = ["银行卡", "WiFi", "APP登录", "网站", "其他"]

# 内存中的密钥缓存 {session_token: (key, expires_at)}
_key_cache: dict[str, tuple[bytes, float]] = {}

SALT = b"vault-pbkdf2-salt-2026"  # 固定 salt，简化实现
CACHE_TIMEOUT = 300  # 5 分钟


def _derive_key(password: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=600000)
    return kdf.derive(password.encode("utf-8"))


def check_master(password: str) -> bool:
    """验证主密码：尝试解密文件"""
    if not STORE.exists():
        return True  # 首次使用，任意密码都接受
    try:
        _decrypt(password)
        return True
    except Exception:
        return False


def _decrypt(password: str) -> list[dict]:
    """用密码解密，返回文档列表"""
    if not STORE.exists():
        return []
    raw = STORE.read_bytes()
    key = _derive_key(password)
    aesgcm = AESGCM(key)
    nonce = raw[:12]
    ciphertext = raw[12:]
    plain = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plain.decode("utf-8"))


def _encrypt(docs: list[dict], password: str):
    """加密并写入文件"""
    key = _derive_key(password)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plain = json.dumps(docs, ensure_ascii=False).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plain, None)
    tmp = STORE.with_suffix(".enc.tmp")
    tmp.write_bytes(nonce + ciphertext)
    os.replace(tmp, STORE)


def _get_key(session_token: str) -> tuple[bytes, bool]:
    """返回 (key, is_valid)，过期自动清理"""
    entry = _key_cache.get(session_token)
    if not entry:
        return b"", False
    key, expires = entry
    if time.time() > expires:
        del _key_cache[session_token]
        return b"", False
    return key, True


def unlock(password: str) -> str | None:
    """解锁：验证密码，缓存密钥，返回 session token"""
    if not check_master(password):
        return None
    token = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
    key = _derive_key(password)
    _key_cache[token] = (key, time.time() + CACHE_TIMEOUT)
    # 清理过期
    expired = [t for t, (_, e) in _key_cache.items() if time.time() > e]
    for t in expired:
        del _key_cache[t]
    return token


def lock(token: str):
    _key_cache.pop(token, None)


def _load(token: str) -> list[dict]:
    key, ok = _get_key(token)
    if not ok:
        raise PermissionError("未解锁或已超时")
    if not STORE.exists():
        return []
    raw = STORE.read_bytes()
    aesgcm = AESGCM(key)
    nonce = raw[:12]
    plain = aesgcm.decrypt(nonce, raw[12:], None)
    return json.loads(plain.decode("utf-8"))


def _save(docs: list[dict], token: str):
    key, ok = _get_key(token)
    if not ok:
        raise PermissionError("未解锁或已超时")
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plain = json.dumps(docs, ensure_ascii=False).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plain, None)
    tmp = STORE.with_suffix(".enc.tmp")
    tmp.write_bytes(nonce + ciphertext)
    os.replace(tmp, STORE)
    # 刷新超时
    _key_cache[token] = (key, time.time() + CACHE_TIMEOUT)


def get_all(token: str) -> dict:
    docs = _load(token)
    by_cat: dict[str, list[dict]] = {}
    for d in docs:
        cat = d.get("category", "其他")
        if cat not in by_cat:
            by_cat[cat] = []
        by_cat[cat].append(d)
    ordered: dict[str, list[dict]] = {}
    for cat in CATEGORIES:
        if cat in by_cat:
            ordered[cat] = by_cat[cat]
    for cat in by_cat:
        if cat not in ordered:
            ordered[cat] = by_cat[cat]
    return {"categories": list(ordered.keys()), "items": ordered, "total": len(docs)}


def add(token: str, category: str, name: str, account: str, password: str, notes: str = "") -> dict:
    docs = _load(token)
    doc = {
        "id": uuid.uuid4().hex[:12],
        "category": category.strip() or "其他",
        "name": name.strip(),
        "account": account.strip(),
        "password": password.strip(),
        "notes": notes.strip(),
        "created_at": datetime.now().isoformat(),
    }
    docs.append(doc)
    _save(docs, token)
    return doc


def update(token: str, item_id: str, category: str = None, name: str = None,
           account: str = None, password: str = None, notes: str = None) -> dict | None:
    docs = _load(token)
    for d in docs:
        if d.get("id") == item_id:
            if category is not None: d["category"] = category.strip() or "其他"
            if name is not None: d["name"] = name.strip()
            if account is not None: d["account"] = account.strip()
            if password is not None: d["password"] = password.strip()
            if notes is not None: d["notes"] = notes.strip()
            _save(docs, token)
            return d
    return None


def delete(token: str, item_id: str) -> bool:
    docs = _load(token)
    new_docs = [d for d in docs if d.get("id") != item_id]
    if len(new_docs) == len(docs):
        return False
    _save(new_docs, token)
    return True
