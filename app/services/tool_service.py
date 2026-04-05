"""
Exécution des tools (function calls) pour OpenAI Realtime.

Quand MIA décide de créer une réservation ou commande, OpenAI envoie un
function_call que ce module exécute. Le flux :
  1. Valider les arguments + vérifier les quotas
  2. Générer un code unique (ex: R4T2K, C7MN9)
  3. Persister en base
  4. Envoyer les SMS (restaurateur + client)
  5. Retourner un récapitulatif vocal que MIA lira au client
"""
import logging
import secrets
from datetime import date

from sqlalchemy import func

from app.database import SessionLocal
from app.models import Commande, Reservation
from app.services.sms_service import (
    notify_client_commande,
    notify_client_reservation,
    notify_restaurant_commande,
    notify_restaurant_reservation,
)

log = logging.getLogger("mia.tools")

# Alphabet sans caractères ambigus (pas de 0/O, 1/I/L, 5/S, 8/B).
_CODE_CHARS = "234679ACDEFGHJKMNPQRTUVWXYZ"


def _generate_code(prefix: str = "R", length: int = 4) -> str:
    """Génère un code court cryptographiquement sûr (ex: R4T2K)."""
    return f"{prefix}{''.join(secrets.choice(_CODE_CHARS) for _ in range(length))}"


def _spell_code(code: str) -> str:
    """Espace chaque caractère pour que MIA épelle le code clairement."""
    return " ".join(c for c in code)


def execute_tool_call(
    restaurant_id: int,
    name: str,
    arguments: dict,
    *,
    menu: list[dict] | None = None,
    quota_reservations: int | None = None,
    quota_commandes: int | None = None,
    restaurant_phone: str | None = None,
    caller_phone: str | None = None,
    restaurant_name: str = "",
) -> dict:
    """Point d'entrée unique : dispatch le function_call vers le bon handler.

    Le numéro de téléphone du client est injecté automatiquement depuis
    caller_phone (récupéré via Telnyx), jamais demandé par MIA.
    """
    args = dict(arguments)
    args["telephone"] = caller_phone or ""

    if name == "create_reservation":
        return _create_reservation(
            restaurant_id, args, quota_reservations, restaurant_phone, restaurant_name,
        )
    if name == "create_commande":
        return _create_commande(
            restaurant_id, args, menu or [], quota_commandes, restaurant_phone, restaurant_name,
        )
    if name == "transfer_to_human":
        return _transfer_to_human(arguments, restaurant_name)

    log.warning("Function call inconnue : %s", name)
    return {"success": False, "error": f"Fonction inconnue : {name}"}


# ──────────────────────────────────────
# Réservation
# ──────────────────────────────────────

def _create_reservation(
    restaurant_id: int,
    args: dict,
    quota: int | None,
    restaurant_phone: str | None,
    restaurant_name: str,
) -> dict:
    date_req = str(args.get("date", ""))
    personnes = max(int(args.get("personnes", 1)), 1)
    heure = str(args.get("heure", ""))
    telephone = args.get("telephone", "")
    code = _generate_code("R")

    db = SessionLocal()
    try:
        # Vérification du quota journalier avant insertion.
        if quota and quota > 0:
            count = db.query(Reservation).filter(
                Reservation.restaurant_id == restaurant_id,
                Reservation.date == date_req,
            ).count()
            if count >= quota:
                return {
                    "success": False,
                    "recap_vocal": (
                        "Désolée, nous n'avons plus de table disponible pour "
                        "cette date. Je peux vous proposer un autre jour si "
                        "vous le souhaitez."
                    ),
                }

        r = Reservation(
            restaurant_id=restaurant_id,
            code=code,
            personnes=personnes,
            heure=heure,
            telephone=telephone,
            date=date_req,
        )
        db.add(r)
        db.commit()
        db.refresh(r)
    except Exception as e:
        db.rollback()
        log.exception("Erreur création réservation : %s", e)
        return {"success": False, "error": str(e)}
    finally:
        db.close()

    # SMS au restaurateur (non-bloquant : un échec n'annule pas la résa).
    if restaurant_phone:
        notify_restaurant_reservation(
            restaurant_phone=restaurant_phone,
            code=code,
            personnes=r.personnes,
            date=r.date,
            heure=r.heure,
            telephone=r.telephone or "",
        )

    # SMS de confirmation au client.
    if r.telephone:
        notify_client_reservation(
            client_phone=r.telephone,
            code=code,
            personnes=r.personnes,
            date=r.date,
            heure=r.heure,
            restaurant_name=restaurant_name,
        )

    log.info("Réservation créée : %s (%d pers. le %s à %s)", code, r.personnes, r.date, r.heure)

    return {
        "success": True,
        "id": r.id,
        "code": code,
        "recap_vocal": (
            f"Parfait ! Votre réservation {_spell_code(code)} est confirmée. "
            f"{r.personnes} personne{'s' if r.personnes > 1 else ''}, "
            f"le {_format_day(r.date)} à {r.heure}. "
            f"Vous allez recevoir un SMS de confirmation. À bientôt !"
        ),
    }


# ──────────────────────────────────────
# Commande à emporter
# ──────────────────────────────────────

def _create_commande(
    restaurant_id: int,
    args: dict,
    menu: list[dict],
    quota: int | None,
    restaurant_phone: str | None,
    restaurant_name: str,
) -> dict:
    telephone = args.get("telephone", "")
    code = _generate_code("C")

    db = SessionLocal()
    try:
        # Quota : compte les commandes du jour (basé sur created_at).
        if quota and quota > 0:
            count = db.query(Commande).filter(
                Commande.restaurant_id == restaurant_id,
                func.date(Commande.created_at) == date.today(),
            ).count()
            if count >= quota:
                return {
                    "success": False,
                    "recap_vocal": (
                        "Désolée, nous avons atteint notre limite de commandes "
                        "pour aujourd'hui. Réessayez demain !"
                    ),
                }

        # Résolution des prix : on match le nom du plat (insensible à la casse)
        # avec le menu chargé en mémoire pour calculer le total.
        prices = {
            m["nom_plat"].strip().lower(): float(m.get("prix", 0))
            for m in menu
            if m.get("nom_plat")
        }
        items_raw = args.get("items", [])
        resolved: list[dict] = []
        total = 0.0
        for it in items_raw:
            plat = str(it.get("plat", "")).strip()
            qty = max(int(it.get("qty", 1)), 1)
            prix_unit = prices.get(plat.lower(), 0)
            resolved.append({"plat": plat, "qty": qty, "prix_unitaire": prix_unit})
            total += qty * prix_unit

        c = Commande(
            restaurant_id=restaurant_id,
            code=code,
            items=resolved,
            prix_total=round(total, 2),
            telephone=telephone,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
    except Exception as e:
        db.rollback()
        log.exception("Erreur création commande : %s", e)
        return {"success": False, "error": str(e)}
    finally:
        db.close()

    items_recap = ", ".join(f"{i['qty']} {i['plat']}" for i in resolved)

    if restaurant_phone:
        notify_restaurant_commande(
            restaurant_phone=restaurant_phone,
            code=code,
            items_recap=items_recap,
            prix_total=c.prix_total,
            telephone=c.telephone or "",
        )

    if c.telephone:
        notify_client_commande(
            client_phone=c.telephone,
            code=code,
            items_recap=items_recap,
            prix_total=c.prix_total,
            restaurant_name=restaurant_name,
        )

    log.info("Commande créée : %s (%.2f€, %d items)", code, c.prix_total, len(resolved))

    return {
        "success": True,
        "id": c.id,
        "code": code,
        "recap_vocal": (
            f"Parfait ! Votre commande {_spell_code(code)} est enregistrée : "
            f"{items_recap}. Total : {c.prix_total:.2f} euros. "
            f"Vous allez recevoir un SMS de confirmation. À tout à l'heure !"
        ),
    }


# ──────────────────────────────────────
# Transfert vers un humain
# ──────────────────────────────────────

def _transfer_to_human(args: dict, restaurant_name: str) -> dict:
    """Prépare le transfert d'appel vers le restaurant.

    Ne fait PAS le transfert Telnyx ici — retourne un flag "transfer": True
    que le bridge (realtime_service) utilise pour déclencher le transfert
    après que MIA a fini de parler au client.
    """
    raison = args.get("raison", "demande client")
    log.info("Transfert demandé : %s (restaurant=%s)", raison, restaurant_name)
    resto = f"du restaurant {restaurant_name}" if restaurant_name else "du restaurant"
    return {
        "success": True,
        "transfer": True,
        "recap_vocal": (
            f"Bien sûr, je vous transfère vers l'équipe {resto}. "
            f"Un instant s'il vous plaît."
        ),
    }


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _format_day(date_str: str) -> str:
    """Extrait le jour du format AAAA-MM-JJ pour le récapitulatif vocal."""
    try:
        parts = str(date_str).strip().split("-")
        if len(parts) >= 3:
            return parts[2].lstrip("0") or "0"
    except Exception:
        pass
    return date_str or ""
