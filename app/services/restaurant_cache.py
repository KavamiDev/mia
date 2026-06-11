"""Cache TTL en mémoire du contexte d'appel : fiche restaurant + menu.

Au décrochage d'un appel, le bridge a besoin du restaurant et de son menu.
Sans cache, chaque appel fait 2 SELECT au moment précis où la latence compte
le plus (le client attend le « Bonjour »). Avec un TTL court + invalidation
explicite sur les mutations (API REST et dashboard), on élimine ces
round-trips DB sans risque de servir un menu périmé plus de TTL secondes.

Le cache stocke des dicts/list simples (pas d'objets ORM) — exactement le
format attendu par run_realtime_bridge. get() retourne des COPIES pour que
le bridge ne puisse pas muter l'entrée partagée.
"""
from __future__ import annotations

import threading
import time

from app.config import settings

_lock = threading.Lock()
# {restaurant_id: (expires_at_monotonic, restaurant_dict, menu_list)}
_cache: dict[int, tuple[float, dict, list[dict]]] = {}


def get(restaurant_id: int) -> tuple[dict, list[dict]] | None:
    """Retourne (restaurant, menu) si présent et non expiré, sinon None."""
    with _lock:
        entry = _cache.get(restaurant_id)
        if entry is None:
            return None
        expires_at, restaurant, menu = entry
        if time.monotonic() >= expires_at:
            del _cache[restaurant_id]
            return None
        return dict(restaurant), [dict(m) for m in menu]


def put(restaurant_id: int, restaurant: dict, menu: list[dict],
        ttl: float | None = None) -> None:
    """Enregistre le contexte d'un restaurant. No-op si le cache est désactivé."""
    ttl = settings.restaurant_cache_ttl_seconds if ttl is None else ttl
    if ttl <= 0:
        return
    with _lock:
        _cache[restaurant_id] = (time.monotonic() + ttl,
                                 dict(restaurant), [dict(m) for m in menu])


def invalidate(restaurant_id: int) -> None:
    """Évince un restaurant après modification de sa fiche ou de son menu."""
    with _lock:
        _cache.pop(restaurant_id, None)


def clear() -> None:
    with _lock:
        _cache.clear()
