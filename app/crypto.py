"""AES 加密/解密工具 — 保护密码和 Cookie"""
import os
import base64
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding


def _get_key():
    key = os.environ.get("CHECKIN_SECRET_KEY", "checkin-default-key-change-me")
    return hashlib.sha256(key.encode()).digest()


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    key = _get_key()
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode()


def decrypt(ciphertext_b64: str) -> str:
    if not ciphertext_b64:
        return ""
    try:
        key = _get_key()
        data = base64.b64decode(ciphertext_b64)
        iv, ciphertext = data[:16], data[16:]
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return plaintext.decode()
    except Exception:
        return ciphertext_b64
