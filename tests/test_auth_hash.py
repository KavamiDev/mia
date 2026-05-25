"""Tests du hashing PBKDF2-SHA256."""
import pytest

from app.utils.auth_hash import hash_password, verify_password


def test_hash_format():
    """Le hash retourné a le format `<salt_b64>$<hash_b64>`."""
    h = hash_password("monpassword")
    parts = h.split("$")
    assert len(parts) == 2
    # Les 2 segments doivent être du base64 valide (longueur multiple de 4)
    assert len(parts[0]) % 4 == 0
    assert len(parts[1]) % 4 == 0


def test_verify_correct_password():
    h = hash_password("hello world")
    assert verify_password("hello world", h) is True


def test_verify_wrong_password():
    h = hash_password("correct horse battery staple")
    assert verify_password("wrong password", h) is False


def test_verify_empty_inputs():
    h = hash_password("real")
    assert verify_password("", h) is False
    assert verify_password("real", "") is False
    assert verify_password("", "") is False


def test_hash_is_salted_unique():
    """Hash 2 fois le même password → résultats différents (sel random)."""
    h1 = hash_password("identical")
    h2 = hash_password("identical")
    assert h1 != h2
    # Mais les deux doivent vérifier
    assert verify_password("identical", h1)
    assert verify_password("identical", h2)


def test_hash_rejects_empty_password():
    """hash_password('') → ValueError (un mot de passe vide ne devrait pas exister)."""
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_handles_garbage_stored():
    """Si le hash stocké est corrompu → False, pas de crash."""
    assert verify_password("test", "not_a_valid_hash") is False
    assert verify_password("test", "no_dollar_separator") is False
    assert verify_password("test", "$$$") is False
