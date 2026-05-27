"""Exécution des function calls OpenAI : réservations, commandes, transfert.

Quand MIA décide d'agir (créer une réservation, une commande, ou transférer
l'appel), OpenAI Realtime émet un `function_call` que ce module traite.

Flux pour réservation/commande :
  1. Validation des arguments + vérification atomique du quota (SELECT FOR UPDATE)
  2. Génération d'un code court unique (ex: R4T2K)
  3. Persistance en base PostgreSQL
  4. Envoi de SMS (selon toggles restaurant) : resto + client
  5. Retour d'un récapitulatif vocal que MIA lit à l'appelant

Économie SMS :
  Brevo facture ~0.045 €/SMS métropole, ~0.10 €/SMS Réunion. Les templates
  ci-dessous sont courts pour rester sous 160 chars (= 1 SMS unique, pas
  multi-part). Les toggles Restaurant.sms_to_* permettent de désactiver
  les envois si le resto consulte le dashboard.
"""
import logging
import secrets
from datetime import date

from sqlalchemy import func

from app.database import SessionLocal
from app.models import Commande, Reservation, Restaurant
from app.services.sms_service import send_sms

log = logging.getLogger("mia.tools")

# Alphabet sans caractères ambigus à l'oral/écrit : pas de 0/O, 1/I/L, 5/S, 8/B.
# Permet d'épeler le code au téléphone sans confusion.
_CODE_CHARS = "234679ACDEFGHJKMNPQRTUVWXYZ"


def _code(prefix: str) -> str:
    """Génère un code court cryptographiquement sûr (ex: R4T2K, C7MN9)."""
    return prefix + "".join(secrets.choice(_CODE_CHARS) for _ in range(4))


def _spell(code: str) -> str:
    """Espace chaque caractère pour épeler le code à l'oral."""
    return " ".join(code)


def _short_date(date_iso: str) -> str:
    """Convertit '2026-06-15' → '15/06'. Si parsing échoue, retourne tel quel.

    Format court utilisé dans les SMS pour économiser des caractères.
    """
    if not date_iso or "-" not in date_iso:
        return date_iso or ""
    parts = date_iso.split("-")
    if len(parts) >= 3:
        # AAAA-MM-JJ → JJ/MM
        return f"{parts[2].lstrip('0').zfill(2)}/{parts[1]}"
    return date_iso


def execute_tool_call(restaurant_id: int, name: str, args: dict, *, menu, quota_reservations,
                      quota_commandes, restaurant_phone, caller_phone, restaurant_name,
                      sms_to_client: bool = True, sms_to_restaurant: bool = True) -> dict:
    """Point d'entrée unique appelé par realtime_service quand OpenAI émet un function_call.

    Args:
        sms_to_client : envoyer un SMS au client (default True, configurable par resto)
        sms_to_restaurant : envoyer un SMS au resto (default True, configurable)
    """
    tel = caller_phone or ""
    if name == "create_reservation":
        return _create_reservation(restaurant_id, args, tel, quota_reservations,
                                   restaurant_phone, restaurant_name,
                                   sms_to_client, sms_to_restaurant)
    if name == "create_commande":
        return _create_commande(restaurant_id, args, tel, menu or [], quota_commandes,
                                restaurant_phone, restaurant_name,
                                sms_to_client, sms_to_restaurant)
    if name == "transfer_to_human":
        return _transfer(args, restaurant_name)
    return {"success": False, "error": f"Fonction inconnue : {name}"}


def _create_reservation(restaurant_id, args, tel, quota, restaurant_phone, restaurant_name,
                        sms_to_client=True, sms_to_restaurant=True) -> dict:
    """Crée une réservation après vérification atomique du quota journalier.

    Pour éviter la race condition (2 appels en // qui voient le quota non
    atteint et insèrent tous les deux), on lock la ligne du restaurant via
    SELECT ... FOR UPDATE le temps du check+insert.
    """
    date_req = str(args.get("date", ""))                # Format AAAA-MM-JJ
    personnes = max(int(args.get("personnes", 1)), 1)   # Au moins 1 personne
    heure = str(args.get("heure", ""))                  # Texte libre (ex: "20h30")
    code = _code("R")

    db = SessionLocal()
    try:
        db.query(Restaurant).filter(Restaurant.id == restaurant_id).with_for_update().first()

        if quota and db.query(Reservation).filter(
            Reservation.restaurant_id == restaurant_id, Reservation.date == date_req
        ).count() >= quota:
            db.commit()
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

    # ─── SMS courts (économie : 1 SMS unique sous 160 chars) ───
    # Format date 15/06 au lieu de 2026-06-15, "Résa" au lieu de "Réservation".
    d = _short_date(date_req)
    if sms_to_restaurant and restaurant_phone:
        # Resto : code + personnes + date + heure + tel client (pour rappel possible)
        # Ex: "Résa R4T2K: 4p le 15/06 20h30. Tel +33692123456" → ~52 chars
        send_sms(restaurant_phone, f"Résa {code}: {personnes}p le {d} {heure}. Tel {tel}")
    if sms_to_client and tel:
        # Client : minimal — code + récap (le restaurant_name est dans le sender MIA)
        # Ex: "Resa R4T2K OK: 4p le 15/06 20h30" → ~33 chars
        send_sms(tel, f"Resa {code} OK: {personnes}p le {d} {heure}")

    log.info("Réservation créée : %s (%d pers. le %s à %s) sms_resto=%s sms_client=%s",
             code, personnes, date_req, heure,
             sms_to_restaurant and bool(restaurant_phone),
             sms_to_client and bool(tel))

    jour = date_req.split("-")[-1].lstrip("0") if "-" in date_req else date_req
    # Le récap vocal mentionne le SMS uniquement si on en envoie un
    sms_msg = " Vous allez recevoir un SMS." if (sms_to_client and tel) else ""
    # Closing naturelle : code + récap + SMS + au revoir chaleureux + invitation à raccrocher.
    # Le client peut alors raccrocher sereinement.
    return {"success": True, "code": code, "recap_vocal":
            f"Parfait ! Votre réservation {_spell(code)} est confirmée. "
            f"{personnes} personne{'s' if personnes > 1 else ''}, le {jour} à {heure}."
            f"{sms_msg} Merci de votre appel, à bientôt, bonne journée !"}


def _create_commande(restaurant_id, args, tel, menu, quota, restaurant_phone, restaurant_name,
                     sms_to_client=True, sms_to_restaurant=True) -> dict:
    """Crée une commande à emporter après calcul du total via le menu."""
    code = _code("C")
    prices = {m["nom_plat"].strip().lower(): float(m.get("prix", 0))
              for m in menu if m.get("nom_plat")}

    db = SessionLocal()
    try:
        db.query(Restaurant).filter(Restaurant.id == restaurant_id).with_for_update().first()

        if quota and db.query(Commande).filter(
            Commande.restaurant_id == restaurant_id,
            func.date(Commande.created_at) == date.today(),
        ).count() >= quota:
            db.commit()
            return {"success": False, "recap_vocal":
                    "Désolée, nous avons atteint notre limite de commandes pour aujourd'hui. Réessayez demain !"}

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

    # ─── SMS courts ───
    # Format compact items : "2x Pizza, 1x Coca" (vs "2 Pizza Margherita, 1 Coca")
    recap_short = ", ".join(f"{i['qty']}x {i['plat']}" for i in resolved)
    recap_full = ", ".join(f"{i['qty']} {i['plat']}" for i in resolved)

    if sms_to_restaurant and restaurant_phone:
        # Resto : code + items + total + tel client
        # Ex: "Cmd C7XYZ: 2x Pizza Reine, 1x Coca. 31€. Tel +33692123456" → ~63 chars
        send_sms(restaurant_phone,
                 f"Cmd {code}: {recap_short}. {total:.0f}€. Tel {tel}")
    if sms_to_client and tel:
        # Client : code + total uniquement (le détail est dans le récap vocal)
        # Ex: "Cmd C7XYZ OK: 31€. À récupérer au resto." → ~40 chars
        send_sms(tel, f"Cmd {code} OK: {total:.0f}€. À récupérer au resto.")

    log.info("Commande créée : %s (%.2f€, %d items) sms_resto=%s sms_client=%s",
             code, total, len(resolved),
             sms_to_restaurant and bool(restaurant_phone),
             sms_to_client and bool(tel))

    sms_msg = " Vous allez recevoir un SMS." if (sms_to_client and tel) else ""
    return {"success": True, "code": code, "recap_vocal":
            f"Parfait ! Votre commande {_spell(code)} est enregistrée : {recap_full}. "
            f"Total : {total:.2f} euros.{sms_msg} Merci, à tout à l'heure, bonne journée !"}


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
