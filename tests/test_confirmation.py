"""Tests du détecteur de confirmation orale (anti-SAV).

Vérifie que has_confirmation/has_rejection/is_confirmed reconnaissent les
variantes courantes du français parlé (accents, casse, ponctuation, syntaxe).
"""
import pytest

from app.utils.confirmation import (
    expects_short_reply,
    has_confirmation,
    has_rejection,
    is_confirmed,
)


# ─────────────────────────────────────────
# Confirmations explicites
# ─────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "oui",
    "Oui",
    "OUI",
    "oui !",
    "Oui, c'est ça",
    "ouais",
    "ok",
    "Ok parfait",
    "d'accord",
    "D'accord",
    "c'est bon",
    "C'est bon pour moi",
    "c'est ça",
    "validez s'il vous plaît",
    "Je valide",
    "Vous pouvez valider",
    "confirmé",
    "Je confirme",
    "Confirmez",
    "parfait",
    "Parfait, merci",
    "exact",
    "exactement",
    "tout à fait",
    "absolument",
    "Bien sûr",
    "correct",
])
def test_has_confirmation_positive(text):
    """Toutes ces formulations doivent être détectées comme confirmations."""
    assert has_confirmation(text), f"« {text} » devrait être une confirmation"


@pytest.mark.parametrize("text", [
    "bonjour",
    "je voudrais réserver",
    "vous avez quoi au menu ?",
    "une pizza margherita",
    "demain à 20h",
    "merci",
    "",
])
def test_has_confirmation_negative(text):
    """Ces textes ne contiennent pas de confirmation explicite."""
    assert not has_confirmation(text), f"« {text} » NE devrait PAS être une confirmation"


# ─────────────────────────────────────────
# Rejets explicites
# ─────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "non",
    "Non",
    "Non merci",
    "annule",
    "annulez",
    "annuler",
    "stop",
    "Stop !",
    "attendez",
    "Attendez un instant",
    "pas du tout",
    "jamais",
    "négatif",
])
def test_has_rejection_positive(text):
    assert has_rejection(text), f"« {text} » devrait être un rejet"


@pytest.mark.parametrize("text", [
    "oui",
    "c'est bon",
    "bonjour",
    "merci",
    "",
])
def test_has_rejection_negative(text):
    assert not has_rejection(text), f"« {text} » NE devrait PAS être un rejet"


# ─────────────────────────────────────────
# is_confirmed : ET logique (confirmé ET pas rejeté)
# ─────────────────────────────────────────


def test_is_confirmed_pure_yes():
    """OUI clair → confirmé."""
    assert is_confirmed("Oui c'est bon")


def test_is_confirmed_ambiguous_yes_but():
    """« Oui mais attendez » → AMBIGU → NON confirmé (la prudence prévaut)."""
    assert not is_confirmed("Oui mais attendez")


def test_is_confirmed_pure_no():
    """NON clair → non confirmé."""
    assert not is_confirmed("Non, c'est pas ça")


def test_is_confirmed_empty():
    assert not is_confirmed("")
    assert not is_confirmed(None)  # type: ignore[arg-type]


# ─────────────────────────────────────────
# Robustesse : accents, ponctuation, casse
# ─────────────────────────────────────────


def test_handles_accents():
    """Les accents ne doivent pas bloquer la détection."""
    assert has_confirmation("validé")
    assert has_confirmation("confirmé")
    assert has_rejection("négatif")


def test_handles_punctuation():
    """Ponctuation diverse → toujours détecté."""
    assert has_confirmation("Oui !!!")
    assert has_confirmation("oui...")
    assert has_confirmation("oui, c'est ça.")


def test_handles_case_insensitive():
    assert has_confirmation("OUI")
    assert has_confirmation("oUi")
    assert has_rejection("NON")


def test_word_boundary_no_match():
    """« oui » ne matche pas comme substring d'un autre mot (\\b boundary)."""
    # « oui » est un mot entier ici → doit matcher
    assert has_confirmation("oui parfait")
    # « ouïe » contient les lettres mais pas le mot « oui » entier
    # (séparateurs : début + fin)
    assert not has_confirmation("ouïe la la")
    # « non » dans « renoncer » ne doit PAS matcher (pas de boundary)
    assert not has_rejection("renoncer ce projet")
    # En revanche « pas non plus » contient le mot « non » entier → match
    assert has_rejection("pas non plus")


# ─────────────────────────────────────────
# expects_short_reply (VAD adaptatif)
# ─────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Je récap : 4 personnes demain à 20h. Je valide ?",
    "Donc 2 pizzas Reine et un Coca. On valide ?",
    "Vous me confirmez par un OUI s'il vous plaît ?",
    "Vous êtes toujours là ? Vous me confirmez avec un OUI ?",
    "6 personnes vendredi à 19h, c'est bien ça ?",
    "Je récapitule : une Margherita à emporter. Je confirme ?",
])
def test_expects_short_reply_on_validation_questions(text):
    """Les questions de validation de MIA → réponse courte attendue."""
    assert expects_short_reply(text)


@pytest.mark.parametrize("text", [
    "",
    "Bonjour, bienvenue chez Marco, MIA à l'appareil.",
    "Pour combien de personnes ?",
    "Nous avons la Margherita à 12 euros et la Reine à 14 euros.",
    "Parfait ! Votre réservation R 4 T 2 K est confirmée. Bonne journée !",
    # « je valide » en DÉBUT d'un long tour de clôture : pas une question.
    "Je valide tout de suite votre demande et je vous donne le code, "
    "un instant s'il vous plaît, je vérifie nos disponibilités pour "
    "vendredi soir et je reviens vers vous immédiatement avec la réponse.",
])
def test_expects_short_reply_negative(text):
    """Les autres tours de MIA ne déclenchent pas le mode réponse courte."""
    assert not expects_short_reply(text)


def test_expects_short_reply_handles_accents_and_case():
    assert expects_short_reply("JE VALIDE ?")
    assert expects_short_reply("c'est bien ça ?")
