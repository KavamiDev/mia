"""
Utilitaires de normalisation téléphonique.

Format cible E.164 :
  - Métropole : +33 6 12 34 56 78
  - La Réunion mobile (0692/0693) : +262 692 ...
  - La Réunion fixe (0262/0263)   : +262 262 ...
  - Mayotte mobile (0639) / fixe (0269) : +262 639 / +262 269

Pourquoi distinguer Réunion / métropole : Telnyx ne livre pas de SMS vers
+262, donc on doit savoir router ces numéros vers le provider de fallback
(OVH SMS). Avant ce fix, 0692xxx était mappé à +33692xxx (faux numéro
métropolitain), ce qui faisait échouer 100 % des SMS Réunion silencieusement.
"""

# Préfixes locaux (à 4 chiffres) appartenant au plan de numérotation Réunion/Mayotte.
# Quand on rencontre l'un d'eux en 06xx/02xx, on route en +262 au lieu de +33.
# Source : ARCEP — plan national de numérotation française.
_REUNION_MAYOTTE_PREFIXES = (
    "0692",  # mobile Réunion
    "0693",  # mobile Réunion (bloc plus récent)
    "0639",  # mobile Mayotte
    "0262",  # fixe Réunion
    "0263",  # fixe Réunion (rare)
    "0269",  # fixe Mayotte
)


def normalize_phone(phone: str | None) -> str | None:
    """Supprime espaces, tirets et guillemets parasites."""
    if not phone:
        return None
    s = str(phone).replace(" ", "").replace("-", "").strip().strip("'\"")
    return s if s else None


def to_e164(phone: str | None, default_country: str = "FR") -> str | None:
    """Convertit un numéro local en E.164.

    Exemples :
      0612345678   → +33612345678   (mobile métropole)
      0692123456   → +262692123456  (mobile Réunion — pas +33 !)
      0262123456   → +262262123456  (fixe Réunion)
      0639xxxxxx   → +262639xxxxxx  (mobile Mayotte)
      +33612345678 → +33612345678   (déjà E.164)
    """
    s = normalize_phone(phone)
    if not s:
        return None
    if s.startswith("+"):
        return s

    if default_country == "FR" and s.startswith("0") and len(s) == 10:
        # Cas spéciaux DOM Océan Indien avant le mapping par défaut +33.
        if s[:4] in _REUNION_MAYOTTE_PREFIXES:
            return "+262" + s[1:]
        # Métropole : 0X + 9 chiffres → +33 + 9 chiffres
        return "+33" + s[1:]

    if default_country == "FR" and s.startswith("33") and len(s) == 11:
        return "+" + s

    # Numéro mobile métropole sans préfixe 0 (ex: 612345678).
    if default_country == "FR" and len(s) == 9 and s[0] in "67":
        return "+33" + s

    # Fallback : préfixe + si chiffre, sinon retourne brut.
    return "+" + s if not s.startswith("+") else s


def matches_phone(stored: str | None, incoming: str | None) -> bool:
    """Compare deux numéros après normalisation (ignore espaces/tirets)."""
    if not stored or not incoming:
        return False
    return normalize_phone(stored) == normalize_phone(incoming)


def is_reunion_mayotte(phone_e164: str | None) -> bool:
    """True si le numéro E.164 est en zone +262 (Réunion + Mayotte).

    Utilisé par sms_service pour router vers OVH SMS (Telnyx ne couvre pas).
    """
    return bool(phone_e164) and phone_e164.startswith("+262")
