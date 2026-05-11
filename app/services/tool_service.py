"""Exécution des function calls OpenAI : réservations, commandes, transfert.

Quand MIA décide d'agir (créer une réservation, une commande, ou transférer
l'appel), OpenAI Realtime émet un `function_call` que ce module traite.

Flux pour réservation/commande :
  1. Validation des arguments + vérification du quota journalier
  2. Génération d'un code court unique (ex: R4T2K)
  3. Persistance en base PostgreSQL
  4. Envoi de 2 SMS : un au restaurateur (détails), un au client (confirmation)
  5. Retour d'un récapitulatif vocal que MIA lit à l'appelant
"""
import logging
import secrets
from datetime import date

from sqlalchemy import func

from app.database import SessionLocal
from app.models import Commande, Reservation
from app.services.sms_service import send_sms

log = logging.getLogger("mia.tools")

# Alphabet sans caractères ambigus à l'oral/écrit : pas de 0/O, 1/I/L, 5/S, 8/B.
# Permet d'épeler le code au téléphone sans confusion.
_CODE_CHARS = "234679ACDEFGHJKMNPQRTUVWXYZ"


def _code(prefix: str) -> str:
    """Génère un code court cryptographiquement sûr (ex: R4T2K, C7MN9).

    `secrets` est utilisé plutôt que `random` car ces codes servent
    d'identifiants exposés au client : un code prévisible pourrait
    permettre de deviner les réservations d'autres clients.
    """
    return prefix + "".join(secrets.choice(_CODE_CHARS) for _ in range(4))


def _spell(code: str) -> str:
    """Espace chaque caractère pour que MIA épelle le code lisiblement
    (« R 4 T 2 K » au lieu de « R4T2K » prononcé d'un bloc)."""
    return " ".join(code)


def execute_tool_call(restaurant_id: int, name: str, args: dict, *, menu, quota_reservations,
                      quota_commandes, restaurant_phone, caller_phone, restaurant_name) -> dict:
    """Point d'entrée unique appelé par realtime_service quand OpenAI émet un function_call.

    Le téléphone du client est injecté ici depuis caller_phone (récupéré via
    Telnyx) — MIA ne le demande JAMAIS à l'oral (cf. prompt système).

    Retourne un dict avec :
      - success: bool
      - recap_vocal: phrase que MIA lira au client
      - transfer: True si on doit transférer l'appel (transfer_to_human)
    """
    tel = caller_phone or ""
    if name == "create_reservation":
        return _create_reservation(restaurant_id, args, tel, quota_reservations, restaurant_phone, restaurant_name)
    if name == "create_commande":
        return _create_commande(restaurant_id, args, tel, menu or [], quota_commandes, restaurant_phone, restaurant_name)
    if name == "transfer_to_human":
        return _transfer(args, restaurant_name)
    return {"success": False, "error": f"Fonction inconnue : {name}"}


def _create_reservation(restaurant_id, args, tel, quota, restaurant_phone, restaurant_name) -> dict:
    """Crée une réservation après vérification du quota journalier."""
    date_req = str(args.get("date", ""))                # Format AAAA-MM-JJ
    personnes = max(int(args.get("personnes", 1)), 1)   # Au moins 1 personne
    heure = str(args.get("heure", ""))                  # Texte libre (ex: "20h30")
    code = _code("R")

    db = SessionLocal()
    try:
        # Vérification du quota AVANT insertion : un restaurant peut limiter
        # le nombre de réservations par jour pour éviter la surcharge.
        if quota and db.query(Reservation).filter(
            Reservation.restaurant_id == restaurant_id, Reservation.date == date_req
        ).count() >= quota:
            return {"success": False, "recap_vocal":
                    "Désolée, plus de table disponible pour cette date. Je peux vous proposer un autre jour ?"}

        r = Reservation(restaurant_id=restaurant_id, code=code, personnes=personnes,
                        heure=heure, telephone=tel, date=date_req)
        db.add(r); db.commit()
    except Exception as e:
        db.rollback()
        log.exception("Erreur réservation : %s", e)
        return {"success": False, "error": str(e)}
    finally:
        db.close()

    # SMS au restaurateur (détails opérationnels) puis au client (confirmation).
    # Les échecs SMS sont silencieux côté send_sms — ne bloquent pas la résa.
    if restaurant_phone:
        send_sms(restaurant_phone,
                 f"Réservation {code}\n{personnes} personnes\n{heure} le {date_req}\nTel : {tel}")
    if tel:
        resto = f" chez {restaurant_name}" if restaurant_name else ""
        send_sms(tel,
                 f"Réservation {code} confirmée{resto}\n{personnes} pers. le {date_req} à {heure}\nÀ bientôt !")

    log.info("Réservation créée : %s (%d pers. le %s à %s)", code, personnes, date_req, heure)

    # Extrait le jour de la date ISO pour un récap vocal plus naturel
    # ("le 5 à 20h" plutôt que "le 2026-05-05 à 20h").
    jour = date_req.split("-")[-1].lstrip("0") if "-" in date_req else date_req
    return {"success": True, "code": code, "recap_vocal":
            f"Parfait ! Votre réservation {_spell(code)} est confirmée. "
            f"{personnes} personne{'s' if personnes > 1 else ''}, le {jour} à {heure}. "
            f"Vous allez recevoir un SMS. À bientôt !"}


def _create_commande(restaurant_id, args, tel, menu, quota, restaurant_phone, restaurant_name) -> dict:
    """Crée une commande à emporter après calcul du total via le menu."""
    code = _code("C")
    # Index du menu pour résoudre rapidement le prix de chaque plat.
    # Match insensible à la casse pour tolérer les variantes de transcription
    # (ex: "Pizza Reine" vs "pizza reine").
    prices = {m["nom_plat"].strip().lower(): float(m.get("prix", 0))
              for m in menu if m.get("nom_plat")}

    db = SessionLocal()
    try:
        # Quota basé sur created_at (date du jour côté serveur, pas date demandée).
        if quota and db.query(Commande).filter(
            Commande.restaurant_id == restaurant_id,
            func.date(Commande.created_at) == date.today(),
        ).count() >= quota:
            return {"success": False, "recap_vocal":
                    "Désolée, nous avons atteint notre limite de commandes pour aujourd'hui. Réessayez demain !"}

        # Résolution des items : on garde le prix unitaire trouvé (0 si plat inconnu).
        # MIA est censée ne proposer que les plats du menu (cf. prompt système).
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

    # Récap textuel "2 Pizza Reine, 1 Coca" pour le SMS et la voix.
    recap = ", ".join(f"{i['qty']} {i['plat']}" for i in resolved)
    if restaurant_phone:
        send_sms(restaurant_phone,
                 f"Commande {code}\n{recap}\nTotal : {total:.2f}€\nTel : {tel}")
    if tel:
        resto = f" chez {restaurant_name}" if restaurant_name else ""
        send_sms(tel,
                 f"Commande {code} confirmée{resto}\n{recap}\nTotal : {total:.2f}€\nÀ récupérer au restaurant !")

    log.info("Commande créée : %s (%.2f€, %d items)", code, total, len(resolved))
    return {"success": True, "code": code, "recap_vocal":
            f"Parfait ! Votre commande {_spell(code)} est enregistrée : {recap}. "
            f"Total : {total:.2f} euros. Vous allez recevoir un SMS. À tout à l'heure !"}


def _transfer(args, restaurant_name) -> dict:
    """Prépare le transfert vers un humain — ne le déclenche PAS ici.

    Renvoie un flag `transfer: True` que le bridge realtime utilise pour
    appeler telnyx_transfer APRÈS que MIA ait fini de dire « je vous
    transfère, un instant ». Sans ce délai, le client serait coupé en
    plein milieu de la phrase de transition.
    """
    log.info("Transfert demandé : %s", args.get("raison", "?"))
    resto = f"du restaurant {restaurant_name}" if restaurant_name else "du restaurant"
    return {"success": True, "transfer": True,
            "recap_vocal": f"Bien sûr, je vous transfère vers l'équipe {resto}. Un instant s'il vous plaît."}
