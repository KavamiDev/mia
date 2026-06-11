"""Rate limiter en mémoire à fenêtre glissante.

Utilisé pour protéger le webhook Telnyx /voice/incoming : la vérification de
signature Ed25519 + le lookup restaurant en DB ont un coût, et un flood de
webhooks forgés peut saturer le process qui sert aussi l'audio temps réel.

Choix d'implémentation :
  - En mémoire (pas de Redis) : MIA tourne en mono-process, et même en
    multi-process la limite par worker reste une protection suffisante.
  - Fenêtre glissante par clé (IP source) via deque de timestamps : précis,
    O(1) amorti, pas de « burst » au reset comme avec une fenêtre fixe.
  - Thread-safe : le webhook est servi par la boucle asyncio mais le limiter
    reste utilisable depuis des threads (asyncio.to_thread).
"""
from __future__ import annotations

import threading
import time
from collections import deque

# Au-delà de ce nombre de clés on purge les entrées périmées pour borner la
# mémoire (un attaquant qui spoofe des IPs ne doit pas faire gonfler le dict).
_PRUNE_THRESHOLD = 1024


class SlidingWindowRateLimiter:
    """Limite le nombre d'événements par clé sur une fenêtre glissante.

    Usage :
        limiter = SlidingWindowRateLimiter(max_events=300, window_seconds=60)
        if not limiter.allow(client_ip):
            return 429
    """

    def __init__(self, max_events: int, window_seconds: float,
                 clock=time.monotonic) -> None:
        if max_events < 1:
            raise ValueError("max_events doit être >= 1")
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._clock = clock  # injectable pour les tests
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """True si l'événement est accepté pour cette clé, False si rejeté."""
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._events.get(key)
            if q is None:
                q = deque()
                self._events[key] = q
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            if len(self._events) > _PRUNE_THRESHOLD:
                self._prune(cutoff)
            return True

    def _prune(self, cutoff: float) -> None:
        """Supprime les clés sans événement récent (appelé sous lock)."""
        stale = [k for k, q in self._events.items() if not q or q[-1] <= cutoff]
        for k in stale:
            del self._events[k]
