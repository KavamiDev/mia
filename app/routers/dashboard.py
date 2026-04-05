"""
Dashboard admin — interface web pour gérer restaurants, menus, réservations, commandes.

Authentification :
  - Login par mot de passe unique (DASHBOARD_PASSWORD dans .env)
  - Session via cookie signé HMAC-SHA256 (DASHBOARD_SECRET dans .env)
  - Cookie httpOnly + SameSite=lax (protection CSRF basique)
  - Toutes les routes vérifient la session via _require_login()

Sécurité :
  - Comparaison timing-safe du mot de passe (hmac.compare_digest)
  - Token signé avec timestamp → expiration automatique (7 jours)
  - Pas de données sensibles dans le cookie (juste timestamp + signature)
"""
import hashlib
import hmac
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Commande, MenuItem, Reservation, Restaurant

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "dashboard" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

COOKIE_NAME = "mia_session"
SESSION_MAX_AGE = 86400 * 7  # 7 jours


# ──────────────────────────────────────
# Auth helpers
# ──────────────────────────────────────

def _sign_token(ts: int) -> str:
    """Crée un token signé 'timestamp:signature' pour le cookie de session."""
    msg = f"mia:{ts}".encode()
    sig = hmac.new(settings.dashboard_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{ts}:{sig}"


def _verify_token(token: str) -> bool:
    """Vérifie un token de session : signature valide + non expiré."""
    try:
        ts_str, _sig = token.split(":", 1)
        ts = int(ts_str)
        if time.time() - ts > SESSION_MAX_AGE:
            return False
        expected = _sign_token(ts)
        return hmac.compare_digest(token, expected)
    except Exception:
        return False


def _is_logged_in(request: Request) -> bool:
    """Vérifie si la requête contient un cookie de session valide."""
    token = request.cookies.get(COOKIE_NAME, "")
    return _verify_token(token)


def _require_login(request: Request) -> RedirectResponse | None:
    """Retourne une redirection vers /login si pas authentifié, sinon None.

    Usage dans chaque route :
        redirect = _require_login(request)
        if redirect:
            return redirect
    """
    if not _is_logged_in(request):
        return RedirectResponse("/dashboard/login", status_code=302)
    return None


# ──────────────────────────────────────
# Login / Logout
# ──────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    # Comparaison timing-safe pour éviter les attaques par analyse temporelle.
    if hmac.compare_digest(password.encode("utf-8"), settings.dashboard_password.encode("utf-8")):
        token = _sign_token(int(time.time()))
        response = RedirectResponse("/dashboard/restaurants", status_code=302)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/dashboard",
        )
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Mot de passe incorrect"})


@router.get("/logout")
async def logout():
    response = RedirectResponse("/dashboard/login", status_code=302)
    response.delete_cookie(COOKIE_NAME, path="/dashboard")
    return response


# ──────────────────────────────────────
# Restaurants
# ──────────────────────────────────────

@router.get("/restaurants", response_class=HTMLResponse)
async def list_restaurants(request: Request, db: Session = Depends(get_db)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    restaurants_raw = db.query(Restaurant).order_by(Restaurant.id).all()
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
        "flash": "", "flash_type": "",
    })


@router.get("/restaurants/new", response_class=HTMLResponse)
async def new_restaurant_form(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse("restaurant_form.html", {
        "request": request, "restaurant": None, "active": "restaurants",
        "flash": "", "flash_type": "",
    })


@router.post("/restaurants/new", response_class=HTMLResponse)
async def create_restaurant(
    request: Request, db: Session = Depends(get_db),
    nom: str = Form(...), telephone: str = Form(...),
    adresse: str = Form(""), horaires: str = Form(""),
    incoming_phone_number: str = Form(""),
    quota_reservations: str = Form(""), quota_commandes: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    r = Restaurant(
        nom=nom.strip(), telephone=telephone.strip(),
        adresse=adresse.strip() or None,
        horaires=horaires.strip() or None,
        incoming_phone_number=incoming_phone_number.strip() or None,
        quota_reservations=_safe_int(quota_reservations),
        quota_commandes=_safe_int(quota_commandes),
    )
    db.add(r)
    db.commit()
    return RedirectResponse(f"/dashboard/restaurants/{r.id}", status_code=302)


@router.get("/restaurants/{restaurant_id}", response_class=HTMLResponse)
async def edit_restaurant_form(request: Request, restaurant_id: int, db: Session = Depends(get_db)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    return templates.TemplateResponse("restaurant_form.html", {
        "request": request, "restaurant": r, "active": "restaurants",
        "flash": "", "flash_type": "",
    })


@router.post("/restaurants/{restaurant_id}", response_class=HTMLResponse)
async def update_restaurant(
    request: Request, restaurant_id: int, db: Session = Depends(get_db),
    nom: str = Form(...), telephone: str = Form(...),
    adresse: str = Form(""), horaires: str = Form(""),
    incoming_phone_number: str = Form(""),
    quota_reservations: str = Form(""), quota_commandes: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    r.nom = nom.strip()
    r.telephone = telephone.strip()
    r.adresse = adresse.strip() or None
    r.horaires = horaires.strip() or None
    r.incoming_phone_number = incoming_phone_number.strip() or None
    r.quota_reservations = _safe_int(quota_reservations)
    r.quota_commandes = _safe_int(quota_commandes)
    db.commit()
    return templates.TemplateResponse("restaurant_form.html", {
        "request": request, "restaurant": r, "active": "restaurants",
        "flash": "Restaurant mis à jour", "flash_type": "success",
    })


# ──────────────────────────────────────
# Menu
# ──────────────────────────────────────

@router.get("/restaurants/{restaurant_id}/menu", response_class=HTMLResponse)
async def menu_page(request: Request, restaurant_id: int, db: Session = Depends(get_db)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        return RedirectResponse("/dashboard/restaurants", status_code=302)
    items = db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id).order_by(MenuItem.nom_plat).all()
    return templates.TemplateResponse("menu.html", {
        "request": request, "restaurant": r, "items": items, "active": "restaurants",
        "flash": "", "flash_type": "",
    })


@router.post("/restaurants/{restaurant_id}/menu", response_class=HTMLResponse)
async def add_menu_item(
    request: Request, restaurant_id: int, db: Session = Depends(get_db),
    nom_plat: str = Form(...), prix: float = Form(...), description: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if prix <= 0:
        return RedirectResponse(f"/dashboard/restaurants/{restaurant_id}/menu", status_code=302)
    m = MenuItem(
        restaurant_id=restaurant_id,
        nom_plat=nom_plat.strip(),
        prix=prix,
        description=description.strip() or None,
    )
    db.add(m)
    db.commit()
    return RedirectResponse(f"/dashboard/restaurants/{restaurant_id}/menu", status_code=302)


@router.post("/restaurants/{restaurant_id}/menu/{item_id}/delete")
async def delete_menu_item(request: Request, restaurant_id: int, item_id: int, db: Session = Depends(get_db)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    m = db.query(MenuItem).filter(MenuItem.id == item_id, MenuItem.restaurant_id == restaurant_id).first()
    if m:
        db.delete(m)
        db.commit()
    return RedirectResponse(f"/dashboard/restaurants/{restaurant_id}/menu", status_code=302)


# ──────────────────────────────────────
# Réservations (lecture seule)
# ──────────────────────────────────────

@router.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request, restaurant_id: int | None = None, db: Session = Depends(get_db)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    restaurants = db.query(Restaurant).order_by(Restaurant.nom).all()
    q = db.query(Reservation).join(Restaurant)
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
        "flash": "", "flash_type": "",
    })


# ──────────────────────────────────────
# Commandes (lecture seule)
# ──────────────────────────────────────

@router.get("/commandes", response_class=HTMLResponse)
async def commandes_page(request: Request, restaurant_id: int | None = None, db: Session = Depends(get_db)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    restaurants = db.query(Restaurant).order_by(Restaurant.nom).all()
    q = db.query(Commande).join(Restaurant)
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
        "flash": "", "flash_type": "",
    })


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _safe_int(value: str) -> int | None:
    """Convertit une valeur de formulaire en int, ou None si vide/invalide."""
    try:
        v = int(value.strip())
        return v if v > 0 else None
    except (ValueError, AttributeError):
        return None
