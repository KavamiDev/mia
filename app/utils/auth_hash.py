"""Hashing de mots de passe — PBKDF2-SHA256 100 000 itérations.

Pourquoi pas bcrypt/argon2 :
  - Stdlib uniquement, pas de dépendance native qui complique le déploiement
  - 100k itérations PBKDF2-SHA256 = recommandation OWASP 2023, suffisant pour
    un dashboard admin avec un nombre limité d'utilisateurs

Format stocké : `<salt_base64>$<hash_base64>` (2 segments séparés par `$`).
"""
import base64
import hashlib
import hmac
import secrets

_ITERATIONS = 100_000
_SALT_BYTES = 16
_HASH_BYTES = 32


def hash_password(password: str) -> str:
    """Génère un hash salé d'un mot de passe.

    Renvoie une chaîne `<salt>$<hash>` (base64) prête à stocker en DB.
    """
    if not password:
        raise ValueError("password ne peut pas être vide")
    salt = secrets.token_bytes(_SALT_BYTES)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                            _ITERATIONS, dklen=_HASH_BYTES)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Vérifie un mot de passe contre un hash stocké. Timing-safe."""
    if not password or not stored or "$" not in stored:
        return False
    try:
        salt_b64, hash_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        if not expected:
            return False
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                       _ITERATIONS, dklen=len(expected))
        return hmac.compare_digest(expected, computed)
    except Exception:
        # base64 invalide, longueurs anormales, etc. — refuse silencieusement.
        return False
