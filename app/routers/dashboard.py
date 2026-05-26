"""
Dashboard admin — interface web multi-tenant pour les restaurants.

Authentification (depuis chantier 5 / sprint multi-tenant) :
  - Login par email + mot de passe (table `users`)
  - Cookie signé HMAC-SHA256 contenant `<user_id>:<ts>:<sig>` (DASHBOARD_SECRET)
  - Cookie httpOnly + SameSite=lax (CSRF basique)
  - Helper `_current_user()` retrouve le User pour chaque requête

Multi-tenant :
  - `user.is_admin == True` → voit/modifie tout
  - `user.restaurant_id` défini  → voit/modifie UNIQUEMENT son restaurant
  - `user.restaurant_id == None` + non-admin → utilisateur orphelin, refusé

Bootstrap :
  - Au startup, si la table `users` est vide, un superadmin est créé
    avec email `admin@mia.local` et le password de DASHBOARD_PASSWORD.
  - Voir `app.main._bootstrap_admin_user()`.

Sécurité :
  - Comparaison timing-safe du mot de passe (hmac.compare_digest via verify_password)
  - Token signé avec timestamp → expiration auto 7 jours
  - Pas de données sensibles dans le cookie (juste user_id + ts + sig)
"""
import hashlib
import hmac
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import CallLog, Commande, MenuItem, Reservation, Restaurant, User
from app.utils.auth_hash import hash_password, verify_password

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "dashboard" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

COOKIE_NAME = "mia_session"
SESSION_MAX_AGE = 86400 * 7  # 7 jours


# ──────────────────────────────────────
# Auth helpers — cookie signé contenant user_id
# ──────────────────────────────────────


def _sign_token(user_id: int, ts: int) -> str:
    """Crée un token signé `<user_id>:<ts>:<sig>`.

    Le user_id est intégré au payload signé, donc impossible à modifier
    sans connaître le secret HMAC.
    """
    msg = f"{user_id}:{ts}".encode()
    sig = hmac.new(settings.dashboard_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{user_id}:{ts}:{sig}"


def _verify_token(token: str) -> int | None:
    """Vérifie un token et retourne le user_id si valide, sinon None."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        user_id_str, ts_str, _ = parts
        user_id = int(user_id_str)
        ts = int(ts_str)
        if time.time() - ts > SESSION_MAX_AGE:
            return None
        expected = _sign_token(user_id, ts)
        if not hmac.compare_digest(token, expected):
            return None
        return user_id
    except (ValueError, AttributeError):
        return None


def _current_user(request: Request, db: Session) -> User | None:
    """Retourne le User de la session, ou None si pas/plus connecté."""
    token = request.cookies.get(COOKIE_NAME, "")
    user_id = _verify_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def _require_user(request: Request, db: Session) -> User | RedirectResponse:
    """Retourne le User connecté OU un redirect vers /login."""
    user = _current_user(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    return user


def _require_admin(user: User) -> None:
    """Lève 403 si l'utilisateur n'est pas admin."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Accès admin requis")


def _user_can_see_restaurant(user: User, restaurant_id: int | None) -> bool:
    """Vérifie qu'un user a le droit de voir/modifier ce restaurant.

    Admin → True pour tout. Sinon, seul SON restaurant_id passe.
    """
    if user.is_admin:
        return True
    return restaurant_id is not None and user.restaurant_id == restaurant_id


def _scope_restaurant_filter(user: User, query, model_restaurant_id_col):
    """Applique le filtre multi-tenant à une query.

    Admin → query inchangée. Sinon force `restaurant_id == user.restaurant_id`.
    Si non-admin sans restaurant_id (orphelin) → filtre sur -1 (résultat vide).
    """
    if user.is_admin:
        return query
    rid = user.restaurant_id if user.restaurant_id is not None else -1
    return query.filter(model_restaurant_id_col == rid)


# ──────────────────────────────────────
# Login / Logout
# ──────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    if _current_user(request, db):
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, db: Session = Depends(get_db),
    email: str = Form(...), password: str = Form(...),
):
    """Vérifie email + password contre la table users."""
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Email ou mot de passe incorrect",
        })

    token = _sign_token(user.id, int(time.time()))
    response = RedirectResponse("/dashboard/restaurants", status_code=302)
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_MAX_AGE,
        httponly=True, samesite="lax", path="/dashboard",
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/dashboard/login", status_code=302)
    response.delete_cookie(COOKIE_NAME, path="/dashboard")
    return response


# ──────────────────────────────────────
# Restaurants (scopé multi-tenant)
# ──────────────────────────────────────


@router.get("/restaurants", response_class=HTMLResponse)
async def list_restaurants(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    q = db.query(Restaurant).order_by(Restaurant.id)
    q = _scope_restaurant_filter(user, q, Restaurant.id)
    restaurants_raw = q.all()

    restaurants = []
    for r in restaurants_raw:
        menu_count = db.query(MenuItem).filter(MenuItem.restaurant_id == r.id).count()
        restaurants.append({
            "id": r.id, "nom": r.nom, "telephone": r.telephone,
            "incoming_phone_number": r.incoming_phone_number,
            "quota_reservations": r.quota_reservations,
            "quota_commandes": r.quota_commandes,
            "menu_count": menu_count,
        })
    return templates.TemplateResponse("restaurants.html", {
        "request": request, "restaurants": restaurants, "active": "restaurants",
        "user": user, "flash": "", "flash_type": "",
    })


@router.get("/restaurants/new", response_class=HTMLResponse)
async def new_restaurant_form(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    _require_admin(user)  # création réservée aux admins
    return templates.TemplateResponse("restaurant_form.html", {
        "request": request, "restaurant": None, "active": "restaurants",
        "user": user, "flash": "", "flash_type": "",
    })


@router.post("/restaurants/new", response_class=HTMLResponse)
async def create_restaurant(
    request: Request, db: Session = Depends(get_db),
    nom: str = Form(...), telephone: str = Form(...),
    adresse: str = Form(""), horaires: str = Form(""),
    incoming_phone_number: str = Form(""),
    quota_reservations: str = Form(""), quota_commandes: str = Form(""),
    sms_to_client: str = Form("on"), sms_to_restaurant: str = Form("on"),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    _require_admin(user)

    r = Restaurant(
        nom=nom.strip(), telephone=telephone.strip(),
        adresse=adresse.strip() or None,
        horaires=horaires.strip() or None,
        incoming_phone_number=incoming_phone_number.strip() or None,
        quota_reservations=_safe_int(quota_reservations),
        quota_commandes=_safe_int(quota_commandes),
        sms_to_client=bool(sms_to_client),
        sms_to_restaurant=bool(sms_to_restaurant),
    )
    db.add(r)
    db.commit()
    return RedirectResponse(f"/dashboard/restaurants/{r.id}", status_code=302)


@router.get("/restaurants/{restaurant_id}", response_class=HTMLResponse)
async def edit_restaurant_form(request: Request, restaurant_id: int, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _user_can_see_restaurant(user, restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit à ce restaurant")
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    return templates.TemplateResponse("restaurant_form.html", {
        "request": request, "restaurant": r, "active": "restaurants",
        "user": user, "flash": "", "flash_type": "",
    })


@router.post("/restaurants/{restaurant_id}", response_class=HTMLResponse)
async def update_restaurant(
    request: Request, restaurant_id: int, db: Session = Depends(get_db),
    nom: str = Form(...), telephone: str = Form(...),
    adresse: str = Form(""), horaires: str = Form(""),
    incoming_phone_number: str = Form(""),
    quota_reservations: str = Form(""), quota_commandes: str = Form(""),
    sms_to_client: str = Form(""), sms_to_restaurant: str = Form(""),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _user_can_see_restaurant(user, restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit à ce restaurant")
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    r.nom = nom.strip()
    r.telephone = telephone.strip()
    r.adresse = adresse.strip() or None
    r.horaires = horaires.strip() or None
    # Champ Telnyx : seul un admin peut le changer (impacte le routage).
    if user.is_admin:
        r.incoming_phone_number = incoming_phone_number.strip() or None
    r.quota_reservations = _safe_int(quota_reservations)
    r.quota_commandes = _safe_int(quota_commandes)
    # Checkboxes : présentes (non-empty string) = activé, absentes = désactivé.
    r.sms_to_client = bool(sms_to_client)
    r.sms_to_restaurant = bool(sms_to_restaurant)
    db.commit()
    return templates.TemplateResponse("restaurant_form.html", {
        "request": request, "restaurant": r, "active": "restaurants",
        "user": user, "flash": "Restaurant mis à jour", "flash_type": "success",
    })


# ──────────────────────────────────────
# Menu (scopé)
# ──────────────────────────────────────

@router.get("/restaurants/{restaurant_id}/menu", response_class=HTMLResponse)
async def menu_page(request: Request, restaurant_id: int, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _user_can_see_restaurant(user, restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit")
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    items = db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id).order_by(MenuItem.nom_plat).all()
    return templates.TemplateResponse("menu.html", {
        "request": request, "restaurant": r, "items": items, "active": "restaurants",
        "user": user, "flash": "", "flash_type": "",
    })


@router.post("/restaurants/{restaurant_id}/menu", response_class=HTMLResponse)
async def add_menu_item(
    request: Request, restaurant_id: int, db: Session = Depends(get_db),
    nom_plat: str = Form(...), prix: float = Form(...), description: str = Form(""),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _user_can_see_restaurant(user, restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit")
    if prix <= 0:
        return RedirectResponse(f"/dashboard/restaurants/{restaurant_id}/menu", status_code=302)
    m = MenuItem(restaurant_id=restaurant_id, nom_plat=nom_plat.strip(), prix=prix,
                 description=description.strip() or None)
    db.add(m)
    db.commit()
    return RedirectResponse(f"/dashboard/restaurants/{restaurant_id}/menu", status_code=302)


@router.post("/restaurants/{restaurant_id}/menu/{item_id}/delete")
async def delete_menu_item(request: Request, restaurant_id: int, item_id: int,
                           db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _user_can_see_restaurant(user, restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit")
    m = db.query(MenuItem).filter(MenuItem.id == item_id,
                                  MenuItem.restaurant_id == restaurant_id).first()
    if m:
        db.delete(m)
        db.commit()
    return RedirectResponse(f"/dashboard/restaurants/{restaurant_id}/menu", status_code=302)


# ──────────────────────────────────────
# Réservations (lecture, scopée)
# ──────────────────────────────────────

@router.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request, restaurant_id: int | None = None,
                            db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Liste des restos affichables dans le sélecteur de filtre.
    rq = db.query(Restaurant).order_by(Restaurant.nom)
    rq = _scope_restaurant_filter(user, rq, Restaurant.id)
    restaurants = rq.all()

    # Si non-admin et restaurant_id demandé n'est pas le sien → force le sien.
    if not user.is_admin and user.restaurant_id is not None:
        restaurant_id = user.restaurant_id

    q = db.query(Reservation).join(Restaurant)
    q = _scope_restaurant_filter(user, q, Reservation.restaurant_id)
    if restaurant_id:
        q = q.filter(Reservation.restaurant_id == restaurant_id)
    rows = q.order_by(Reservation.date.desc(), Reservation.heure.desc()).limit(100).all()

    reservations = []
    for r in rows:
        reservations.append({
            "code": r.code, "date": r.date, "heure": r.heure,
            "personnes": r.personnes, "telephone": r.telephone,
            "restaurant_nom": r.restaurant.nom, "created_at": r.created_at,
        })
    return templates.TemplateResponse("reservations.html", {
        "request": request, "reservations": reservations, "restaurants": restaurants,
        "selected_id": restaurant_id, "active": "reservations",
        "user": user, "flash": "", "flash_type": "",
    })


# ──────────────────────────────────────
# Commandes (lecture, scopée)
# ──────────────────────────────────────

@router.get("/commandes", response_class=HTMLResponse)
async def commandes_page(request: Request, restaurant_id: int | None = None,
                         db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rq = db.query(Restaurant).order_by(Restaurant.nom)
    rq = _scope_restaurant_filter(user, rq, Restaurant.id)
    restaurants = rq.all()

    if not user.is_admin and user.restaurant_id is not None:
        restaurant_id = user.restaurant_id

    q = db.query(Commande).join(Restaurant)
    q = _scope_restaurant_filter(user, q, Commande.restaurant_id)
    if restaurant_id:
        q = q.filter(Commande.restaurant_id == restaurant_id)
    rows = q.order_by(Commande.created_at.desc()).limit(100).all()

    commandes = []
    for c in rows:
        items = c.items or []
        items_display = ", ".join(f"{i.get('qty', 1)} {i.get('plat', '?')}" for i in items)
        commandes.append({
            "code": c.code, "items_display": items_display,
            "prix_total": c.prix_total, "telephone": c.telephone,
            "restaurant_nom": c.restaurant.nom, "created_at": c.created_at,
        })
    return templates.TemplateResponse("commandes.html", {
        "request": request, "commandes": commandes, "restaurants": restaurants,
        "selected_id": restaurant_id, "active": "commandes",
        "user": user, "flash": "", "flash_type": "",
    })


# ──────────────────────────────────────
# Appels (logs SAV) — scopés
# ──────────────────────────────────────

@router.get("/calls", response_class=HTMLResponse)
async def calls_page(
    request: Request, db: Session = Depends(get_db),
    restaurant_id: int | None = None, sav_only: bool = False,
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rq = db.query(Restaurant).order_by(Restaurant.nom)
    rq = _scope_restaurant_filter(user, rq, Restaurant.id)
    restaurants = rq.all()

    if not user.is_admin and user.restaurant_id is not None:
        restaurant_id = user.restaurant_id

    q = db.query(CallLog)
    q = _scope_restaurant_filter(user, q, CallLog.restaurant_id)
    if restaurant_id:
        q = q.filter(CallLog.restaurant_id == restaurant_id)
    if sav_only:
        q = q.filter(CallLog.sav_flagged.is_(True))
    q = q.order_by(CallLog.started_at.desc())

    rmap = {r.id: r.nom for r in db.query(Restaurant).all()}
    rows = []
    for c in q.limit(100).all():
        rows.append({
            "id": c.id,
            "restaurant_nom": rmap.get(c.restaurant_id, "?"),
            "caller_phone": c.caller_phone or "anonyme",
            "started_at": c.started_at,
            "duration_seconds": c.duration_seconds or 0,
            "transcript_count": len(c.transcript or []),
            "tool_count": len(c.tool_calls or []),
            "reservation_code": c.reservation_code,
            "commande_code": c.commande_code,
            "sav_flagged": c.sav_flagged,
        })

    return templates.TemplateResponse("calls.html", {
        "request": request, "calls": rows, "restaurants": restaurants,
        "selected_id": restaurant_id, "sav_only": sav_only,
        "active": "calls", "user": user, "flash": "", "flash_type": "",
    })


@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(request: Request, call_id: int, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    c = db.query(CallLog).filter(CallLog.id == call_id).first()
    if not c:
        return RedirectResponse("/dashboard/calls", status_code=302)
    if not _user_can_see_restaurant(user, c.restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit à cet appel")

    restaurant = db.query(Restaurant).filter(Restaurant.id == c.restaurant_id).first()
    return templates.TemplateResponse("call_detail.html", {
        "request": request, "call": c, "restaurant": restaurant,
        "active": "calls", "user": user, "flash": "", "flash_type": "",
    })


@router.post("/calls/{call_id}/sav", response_class=HTMLResponse)
async def flag_sav(
    request: Request, call_id: int, db: Session = Depends(get_db),
    sav_notes: str = Form(""), unflag: str = Form(""),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    c = db.query(CallLog).filter(CallLog.id == call_id).first()
    if not c:
        return RedirectResponse("/dashboard/calls", status_code=302)
    if not _user_can_see_restaurant(user, c.restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit")

    if unflag:
        c.sav_flagged = False
        c.sav_notes = None
    else:
        c.sav_flagged = True
        c.sav_notes = sav_notes.strip() or None
    db.commit()
    return RedirectResponse(f"/dashboard/calls/{call_id}", status_code=302)


# ──────────────────────────────────────
# Gestion des utilisateurs (admin only)
# ──────────────────────────────────────


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    _require_admin(user)

    users = db.query(User).order_by(User.id).all()
    restaurants = db.query(Restaurant).order_by(Restaurant.nom).all()
    return templates.TemplateResponse("users.html", {
        "request": request, "users": users, "restaurants": restaurants,
        "active": "users", "user": user, "flash": "", "flash_type": "",
    })


@router.post("/users", response_class=HTMLResponse)
async def create_user(
    request: Request, db: Session = Depends(get_db),
    email: str = Form(...), password: str = Form(...),
    restaurant_id: str = Form(""), is_admin: str = Form(""),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    _require_admin(user)

    email_clean = email.strip().lower()
    if db.query(User).filter(User.email == email_clean).first():
        # Email déjà pris → on redirige avec rien (les flashs nécessitent plus de plomberie)
        return RedirectResponse("/dashboard/users", status_code=302)

    new_u = User(
        email=email_clean,
        password_hash=hash_password(password),
        restaurant_id=_safe_int(restaurant_id),
        is_admin=bool(is_admin),
    )
    db.add(new_u)
    db.commit()
    return RedirectResponse("/dashboard/users", status_code=302)


@router.post("/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    _require_admin(user)
    # Empêche un admin de se supprimer lui-même par accident
    if user_id == user.id:
        return RedirectResponse("/dashboard/users", status_code=302)
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        db.delete(u)
        db.commit()
    return RedirectResponse("/dashboard/users", status_code=302)


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _safe_int(value: str) -> int | None:
    """Convertit une valeur de formulaire en int, ou None si vide/invalide."""
    try:
        v = int(str(value).strip())
        return v if v > 0 else None
    except (ValueError, AttributeError):
        return None
