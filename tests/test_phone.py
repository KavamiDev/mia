"""Tests de normalisation téléphonique.

Particulièrement critique pour les préfixes Réunion (0692/0693) qui doivent
être routés en +262 (et non +33) pour que SMS service les envoie via OVH.
"""
from app.utils.phone import is_reunion_mayotte, matches_phone, normalize_phone, to_e164


# ─────────────────────────────────────────
# normalize_phone
# ─────────────────────────────────────────


def test_normalize_strips_spaces_and_dashes():
    assert normalize_phone("06 12 34-56 78") == "0612345678"


def test_normalize_strips_quotes():
    assert normalize_phone("'+33612345678'") == "+33612345678"
    assert normalize_phone('"0612345678"') == "0612345678"


def test_normalize_handles_none_and_empty():
    assert normalize_phone(None) is None
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None


def test_normalize_preserves_plus():
    assert normalize_phone("+33 6 12 34 56 78") == "+33612345678"


# ─────────────────────────────────────────
# to_e164 — métropole
# ─────────────────────────────────────────


def test_e164_metropole_mobile():
    """Mobile 06xx classique → +33."""
    assert to_e164("0612345678") == "+33612345678"
    assert to_e164("0712345678") == "+33712345678"


def test_e164_metropole_fixe():
    """Fixe 01-05 → +33."""
    assert to_e164("0123456789") == "+33123456789"
    assert to_e164("0556789012") == "+33556789012"


def test_e164_already_e164():
    """Numéro déjà en E.164 → inchangé."""
    assert to_e164("+33612345678") == "+33612345678"
    assert to_e164("+262692123456") == "+262692123456"


def test_e164_metropole_without_zero():
    """Mobile sans 0 (612345678) → +33612345678."""
    assert to_e164("612345678") == "+33612345678"
    assert to_e164("712345678") == "+33712345678"


def test_e164_with_33_prefix_no_plus():
    """33612345678 (sans +) → +33612345678."""
    assert to_e164("33612345678") == "+33612345678"


# ─────────────────────────────────────────
# to_e164 — Réunion / Mayotte (le bug que ce module corrige)
# ─────────────────────────────────────────


def test_e164_reunion_mobile_0692():
    """0692 → +262 (et NON +33). C'est le bug qui faisait échouer 100% des SMS RE."""
    assert to_e164("0692123456") == "+262692123456"


def test_e164_reunion_mobile_0693():
    """0693 = bloc mobile Réunion plus récent."""
    assert to_e164("0693456789") == "+262693456789"


def test_e164_reunion_fixe_0262():
    """Fixe Réunion 0262 → +262."""
    assert to_e164("0262123456") == "+262262123456"


def test_e164_mayotte_mobile_0639():
    """Mobile Mayotte (zone +262 partagée avec Réunion)."""
    assert to_e164("0639987654") == "+262639987654"


def test_e164_mayotte_fixe_0269():
    assert to_e164("0269123456") == "+262269123456"


def test_e164_invalid_input():
    """Inputs vides ou None → None, pas de crash."""
    assert to_e164(None) is None
    assert to_e164("") is None


# ─────────────────────────────────────────
# matches_phone
# ─────────────────────────────────────────


def test_matches_ignores_formatting():
    assert matches_phone("06 12 34 56 78", "0612345678") is True
    assert matches_phone("06-12-34-56-78", "06 12 34 56 78") is True


def test_matches_different_numbers():
    assert matches_phone("0612345678", "0612345679") is False


def test_matches_handles_none():
    assert matches_phone(None, "0612345678") is False
    assert matches_phone("0612345678", None) is False
    assert matches_phone(None, None) is False


# ─────────────────────────────────────────
# is_reunion_mayotte (router SMS)
# ─────────────────────────────────────────


def test_is_reunion_true_for_262():
    assert is_reunion_mayotte("+262692123456") is True


def test_is_reunion_false_for_33():
    assert is_reunion_mayotte("+33612345678") is False


def test_is_reunion_false_for_other_countries():
    assert is_reunion_mayotte("+15551234567") is False  # USA
    assert is_reunion_mayotte("+447911123456") is False  # UK


def test_is_reunion_false_for_none_or_empty():
    assert is_reunion_mayotte(None) is False
    assert is_reunion_mayotte("") is False
