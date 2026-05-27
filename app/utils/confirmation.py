"""Détection de confirmation orale dans une transcription FR.

Utilisé par le bridge realtime pour bloquer un tool call (réservation ou
commande) si le client n'a pas dit explicitement OUI / VALIDÉ / etc. juste
avant. Évite les SAV : si MIA invente un OUI ou interprète mal, on coupe.

Approche : matching de mots-clés normalisés (sans casse, sans accent, sans
ponctuation). Plus robuste qu'une regex stricte parce que la transcription
n'est pas toujours propre.
"""
from __future__ import annotations

import re
import unicodedata

# Confirmations explicites — déclenchent l'autorisation d'exécuter le tool.
# On veut un signal CLAIR du client : "oui", "c'est bon", "validez", etc.
# Note : "ok" est volontairement INCLUS car courant à l'oral en France ;
# le risque de faux positif est minime parce que la garde regarde uniquement
# la transcription du DERNIER tour client (après le récap de MIA).
_CONFIRM_PATTERNS = [
    r"\boui\b",
    r"\bouais\b",
    r"\bok\b",
    r"\bd accord\b",        # "d'accord" normalisé
    r"\bc est bon\b",       # "c'est bon"
    r"\bc est ca\b",        # "c'est ça"
    r"\bvalide\b",
    r"\bvalidez\b",
    r"\bvalider\b",
    r"\bconfirme\b",
    r"\bconfirmer\b",
    r"\bconfirmez\b",
    r"\bconfirme\b",        # "confirmé" normalisé sans accent
    r"\bparfait\b",
    r"\bexact\b",
    r"\bexactement\b",
    r"\babsolument\b",
    r"\btout a fait\b",     # "tout à fait"
    r"\bcorrect\b",
    r"\bbien\b",            # "bien sûr", "bien noté"
]

# Rejets explicites OU ambiguïtés — annulent l'action en cours.
# Le client doit donner un OUI franc, pas un "peut-être".
_REJECT_PATTERNS = [
    r"\bnon\b",
    r"\bannule\b",
    r"\bannuler\b",
    r"\bannulez\b",
    r"\bstop\b",
    r"\battendez\b",
    r"\battend\b",
    r"\bpas du tout\b",
    r"\bjamais\b",
    r"\bpas vraiment\b",
    r"\bplutot pas\b",      # "plutôt pas"
    r"\bnegatif\b",         # "négatif"
    # Ambiguïtés — on refuse de valider sur incertitude
    r"\bpeut etre\b",       # "peut-être" (apostrophe → espace par _normalize)
    r"\bptet\b",            # "ptêt"
    r"\bje sais pas\b",     # "je sais pas"
    r"\bje ne sais pas\b",
    r"\bhmm\b",             # hésitation
    r"\beuh\b",
    r"\bje verrais\b",      # "je verrai"
    r"\battends voir\b",
]


def _normalize(text: str) -> str:
    """Lowercase + retire accents + simplifie ponctuation pour matching robuste.

    Exemples :
      "Oui, c'est ça !"        → "oui  c est ca  "
      "Validé, parfait !"      → "valide  parfait  "
      "Bien sûr, allez-y."     → "bien sur  allez y "
    """
    if not text:
        return ""
    # NFKD : décompose les accents (é → e + ´), puis on filtre les diacritiques
    nfkd = unicodedata.normalize("NFKD", text)
    no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lower + remplace apostrophes/tirets/ponctuation par espaces
    normalized = no_accent.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return normalized


def has_confirmation(text: str) -> bool:
    """True si le texte contient au moins un mot de confirmation explicite."""
    if not text:
        return False
    norm = _normalize(text)
    return any(re.search(p, norm) for p in _CONFIRM_PATTERNS)


def has_rejection(text: str) -> bool:
    """True si le texte contient un mot indiquant un refus/pause."""
    if not text:
        return False
    norm = _normalize(text)
    return any(re.search(p, norm) for p in _REJECT_PATTERNS)


def is_confirmed(text: str) -> bool:
    """True si confirmé ET pas rejeté dans le même tour.

    Cas ambigus comme « oui mais attendez » → False (la prudence prévaut).
    """
    return has_confirmation(text) and not has_rejection(text)
