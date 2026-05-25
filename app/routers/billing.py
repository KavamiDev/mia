"""Pages dashboard de facturation.

  - /dashboard/billing            : vue admin globale (tous les restos)
  - /dashboard/billing/<rid>      : détail d'un restaurant (mois courant + historique)

Scope multi-tenant identique aux autres routes : non-admin ne peut voir que
son propre restaurant.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Restaurant
from app.routers.dashboard import _require_user, _user_can_see_restaurant
from app.services.billing_service import all_restaurants_summary, monthly_summary

router = APIRouter(prefix="/dashboard/billing", tags=["billing"])

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "dashboard" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("", response_class=HTMLResponse)
async def billing_index(request: Request, db: Session = Depends(get_db)):
    """Vue d'ensemble : admin voit tous les restos, user voit le sien."""
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Non-admin : redirige direct vers la page de son restaurant
    if not user.is_admin:
        if user.restaurant_id:
            return RedirectResponse(f"/dashboard/billing/{user.restaurant_id}", status_code=302)
        raise HTTPException(status_code=403, detail="Pas de restaurant associé")

    # Admin : agrégat global
    summaries = all_restaurants_summary(db)
    restaurants = {r.id: r for r in db.query(Restaurant).all()}
    rows = []
    for s in summaries:
        r = restaurants.get(s["restaurant_id"])
        rows.append({
            "restaurant_id": s["restaurant_id"],
            "restaurant_nom": r.nom if r else "?",
            "call_count": s["call_count"],
            "total_minutes": round(s["total_seconds"] / 60.0, 1),
            "total_eur": s["total_eur"],
        })
    # Tri décroissant par coût
    rows.sort(key=lambda x: x["total_eur"], reverse=True)

    return templates.TemplateResponse("billing.html", {
        "request": request, "rows": rows,
        "active": "billing", "user": user, "flash": "", "flash_type": "",
    })


@router.get("/{restaurant_id}", response_class=HTMLResponse)
async def billing_detail(request: Request, restaurant_id: int,
                         db: Session = Depends(get_db)):
    """Détail facturation d'un restaurant (mois courant)."""
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _user_can_see_restaurant(user, restaurant_id):
        raise HTTPException(status_code=403, detail="Accès interdit")

    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not restaurant:
        return RedirectResponse("/dashboard/billing", status_code=302)

    summary = monthly_summary(db, restaurant_id)
    summary["total_minutes"] = round(summary["total_seconds"] / 60.0, 1)

    return templates.TemplateResponse("billing_detail.html", {
        "request": request, "restaurant": restaurant, "summary": summary,
        "active": "billing", "user": user, "flash": "", "flash_type": "",
    })
