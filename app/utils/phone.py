"""
Utilitaires de normalisation téléphonique.

Telnyx et l'API SMS exigent le format E.164 (+33612345678).
Ces fonctions gèrent la conversion depuis les formats courants
français (06..., 0692..., etc.) vers E.164.
"""


def normalize_phone(phone: str | None) -> str | None:
    """Supprime espaces, tirets et guillemets parasites."""
    if not phone:
        return None
    s = str(phone).replace(" ", "").replace("-", "").strip().strip("'\"")
    return s if s else None


def to_e164(phone: str | None, default_country: str = "FR") -> str | None:
    """Convertit un numéro local en E.164 pour Telnyx.

    Exemples :
      0612345678   → +33612345678
      0692123456   → +33692123456  (Réunion)
      +33612345678 → +33612345678  (déjà E.164)
    """
    s = normalize_phone(phone)
    if not s:
        return None
    if s.startswith("+"):
        return s
    if default_country == "FR" and len(s) >= 9:
        if s.startswith("0"):
            return "+33" + s[1:]
        if s.startswith("33") and len(s) == 11:
            return "+" + s
        # Numéro mobile sans préfixe 0 (ex: 612345678)
        if len(s) == 9 and s[0] in "67":
            return "+33" + s
    return "+" + s if not s.startswith("+") else s


def matches_phone(stored: str | None, incoming: str | None) -> bool:
    """Compare deux numéros après normalisation (ignore espaces/tirets)."""
    if not stored or not incoming:
        return False
    return normalize_phone(stored) == normalize_phone(incoming)
