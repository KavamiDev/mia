"""Exécution des function calls OpenAI : réservations, commandes, transfert."""
import logging
import secrets
from datetime import date

from sqlalchemy import func

from app.database import SessionLocal
from app.models import Commande, Reservation
from app.services.sms_service import send_sms

log = logging.getLogger("mia.tools")

# Alphabet sans caractères ambigus (pas de 0/O, 1/I/L, 5/S, 8/B).
_CODE_CHARS = "234679ACDEFGHJKMNPQRTUVWXYZ"


def _code(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_CODE_CHARS) for _ in range(4))


def _spell(code: str) -> str:
    return " ".join(code)


def execute_tool_call(restaurant_id: int, name: str, args: dict, *, menu, quota_reservations,
                      quota_commandes, restaurant_phone, caller_phone, restaurant_name) -> dict:
    """Dispatch un function_call vers le handler approprié."""
    tel = caller_phone or ""
    if name == "create_reservation":
        return _create_reservation(restaurant_id, args, tel, quota_reservations, restaurant_phone, restaurant_name)
    if name == "create_commande":
        return _create_commande(restaurant_id, args, tel, menu or [], quota_commandes, restaurant_phone, restaurant_name)
    if name == "transfer_to_human":
        return _transfer(args, restaurant_name)
    return {"success": False, "error": f"Fonction inconnue : {name}"}


def _create_reservation(restaurant_id, args, tel, quota, restaurant_phone, restaurant_name) -> dict:
    date_req = str(args.get("date", ""))
    personnes = max(int(args.get("personnes", 1)), 1)
    heure = str(args.get("heure", ""))
    code = _code("R")

    db = SessionLocal()
    try:
        if quota and db.query(Reservation).filter(
            Reservation.restaurant_id == restaurant_id, Reservation.date == date_req
        ).count() >= quota:
            return {"success": False, "recap_vocal": "Désolée, plus de table disponible pour cette date. Je peux vous proposer un autre jour ?"}
        r = Reservation(restaurant_id=restaurant_id, code=code, personnes=personnes,
                        heure=heure, telephone=tel, date=date_req)
        db.add(r); db.commit()
    except Exception as e:
        db.rollback()
        log.exception("Erreur réservation : %s", e)
        return {"success": False, "error": str(e)}
    finally:
        db.close()

    if restaurant_phone:
        send_sms(restaurant_phone, f"Réservation {code}\n{personnes} personnes\n{heure} le {date_req}\nTel : {tel}")
    if tel:
        resto = f" chez {restaurant_name}" if restaurant_name else ""
        send_sms(tel, f"Réservation {code} confirmée{resto}\n{personnes} pers. le {date_req} à {heure}\nÀ bientôt !")

    log.info("Réservation créée : %s (%d pers. le %s à %s)", code, personnes, date_req, heure)
    jour = date_req.split("-")[-1].lstrip("0") if "-" in date_req else date_req
    return {"success": True, "code": code, "recap_vocal":
            f"Parfait ! Votre réservation {_spell(code)} est confirmée. "
            f"{personnes} personne{'s' if personnes > 1 else ''}, le {jour} à {heure}. "
            f"Vous allez recevoir un SMS. À bientôt !"}


def _create_commande(restaurant_id, args, tel, menu, quota, restaurant_phone, restaurant_name) -> dict:
    code = _code("C")
    prices = {m["nom_plat"].strip().lower(): float(m.get("prix", 0)) for m in menu if m.get("nom_plat")}

    db = SessionLocal()
    try:
        if quota and db.query(Commande).filter(
            Commande.restaurant_id == restaurant_id,
            func.date(Commande.created_at) == date.today(),
        ).count() >= quota:
            return {"success": False, "recap_vocal": "Désolée, nous avons atteint notre limite de commandes pour aujourd'hui. Réessayez demain !"}

        resolved, total = [], 0.0
        for it in args.get("items", []):
            plat = str(it.get("plat", "")).strip()
            qty = max(int(it.get("qty", 1)), 1)
            unit = prices.get(plat.lower(), 0)
            resolved.append({"plat": plat, "qty": qty, "prix_unitaire": unit})
            total += qty * unit

        c = Commande(restaurant_id=restaurant_id, code=code, items=resolved,
                     prix_total=round(total, 2), telephone=tel)
        db.add(c); db.commit()
    except Exception as e:
        db.rollback()
        log.exception("Erreur commande : %s", e)
        return {"success": False, "error": str(e)}
    finally:
        db.close()

    recap = ", ".join(f"{i['qty']} {i['plat']}" for i in resolved)
    if restaurant_phone:
        send_sms(restaurant_phone, f"Commande {code}\n{recap}\nTotal : {total:.2f}€\nTel : {tel}")
    if tel:
        resto = f" chez {restaurant_name}" if restaurant_name else ""
        send_sms(tel, f"Commande {code} confirmée{resto}\n{recap}\nTotal : {total:.2f}€\nÀ récupérer au restaurant !")

    log.info("Commande créée : %s (%.2f€, %d items)", code, total, len(resolved))
    return {"success": True, "code": code, "recap_vocal":
            f"Parfait ! Votre commande {_spell(code)} est enregistrée : {recap}. "
            f"Total : {total:.2f} euros. Vous allez recevoir un SMS. À tout à l'heure !"}


def _transfer(args, restaurant_name) -> dict:
    """Flag « transfer » lu par le bridge pour transférer après que MIA ait fini de parler."""
    log.info("Transfert demandé : %s", args.get("raison", "?"))
    resto = f"du restaurant {restaurant_name}" if restaurant_name else "du restaurant"
    return {"success": True, "transfer": True,
            "recap_vocal": f"Bien sûr, je vous transfère vers l'équipe {resto}. Un instant s'il vous plaît."}
