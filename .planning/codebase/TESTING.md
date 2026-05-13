# Testing Patterns

**Analysis Date:** 2026-05-13

## État actuel : aucun test automatisé

**Détecté :**
- Aucun fichier `test_*.py` ou `*_test.py` dans le dépôt.
- Aucun répertoire `tests/`.
- Aucun fichier `conftest.py`.
- Aucune configuration test : pas de `pytest.ini`, `pyproject.toml`,
  `setup.cfg`, `tox.ini`.
- Aucune dépendance de test dans `requirements.txt` (pas de `pytest`,
  `pytest-asyncio`, `httpx`, `pytest-mock`, `coverage`).
- Aucun pipeline CI (`.github/workflows/`, `.gitlab-ci.yml`, etc.).
- Aucun hook pré-commit (`.pre-commit-config.yaml`).

**Validation actuelle :** purement manuelle (appel test sur Telnyx, vérification
des SMS reçus, inspection du dashboard, lecture des logs `mia.conv`).

## Test Framework

**Aucun framework installé.**

**Recommandation pour future introduction :**
- `pytest` + `pytest-asyncio` pour le code FastAPI/websockets.
- `httpx` + `fastapi.testclient.TestClient` pour les routes REST et dashboard.
- `pytest-mock` ou `unittest.mock` (stdlib) pour les mocks.
- Emplacement : `tests/` à la racine, miroir de `app/` (`tests/services/`,
  `tests/routers/`, `tests/utils/`).
- Config : section `[tool.pytest.ini_options]` dans un nouveau `pyproject.toml`.
- Dépendances test isolées dans `requirements-dev.txt` (ne pas polluer
  `requirements.txt` qui reste minimal — cf. refactor lite -55% LOC).

**Run Commands (à mettre en place) :**
```bash
pytest                        # Run all tests
pytest -k <pattern>           # Sélection par nom
pytest --cov=app --cov-report=term-missing   # Coverage
```

## Test File Organization (proposée)

**Layout suggéré, alignée sur la structure de `app/` :**
```
tests/
├── conftest.py               # Fixtures partagées (DB SQLite mémoire, settings override)
├── test_phone.py             # tests pour app/utils/phone.py (cible facile)
├── test_config.py            # tests des helpers _s/_f/_i
├── routers/
│   ├── test_restaurants.py
│   ├── test_menu.py
│   ├── test_reservations.py
│   └── test_dashboard.py     # auth cookie + accès pages
├── services/
│   ├── test_tool_service.py  # générateur de code, quotas, calcul total
│   ├── test_sms_service.py
│   └── test_telnyx_service.py
└── test_main.py              # health check, startup warnings
```

**Naming proposée :**
- Fichiers : `test_<module>.py`.
- Fonctions : `test_<comportement>` en français OK (cohérent avec la convention
  de docstrings du repo) : `test_to_e164_format_local_francais`,
  `test_creation_reservation_respecte_quota`.

## Test Structure (proposée)

**Modèle pytest fonctionnel (préféré à la classe unittest) :**
```python
def test_to_e164_converti_un_numero_local_en_international():
    assert to_e164("0612345678") == "+33612345678"

def test_to_e164_retourne_none_pour_entree_vide():
    assert to_e164(None) is None
    assert to_e164("") is None
```

**Pour les routes FastAPI :**
```python
from fastapi.testclient import TestClient
from app.main import app

def test_root_retourne_status_ok():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "MIA"}
```

## Cibles prioritaires (zones à fort ROI sans tests actuels)

1. **`app/utils/phone.py`** — pur, déterministe, critique pour Telnyx/SMS.
   Couverture rapide.
2. **`app/services/tool_service.py`** — logique métier centrale
   (génération de code, calcul de total commande, vérification quotas).
   Risque de régression élevé. Nécessite une session SQLAlchemy de test.
3. **`app/auth.py`** — comparaison timing-safe, branche
   "pas de clé configurée" (dev).
4. **`app/routers/dashboard.py`** — login, signature HMAC, expiration de token
   (`_sign_token` / `_verify_token`).
5. **`app/config.py`** — helpers `_s`, `_f`, `_i` (parsing défensif).
6. **`app/routers/restaurants.py`** + autres CRUD — happy path + 404.

**Non-cible immédiate :** `app/services/realtime_service.py` (bridge
WebSocket bidirectionnel asyncio) — test d'intégration complexe, mocking
extensif des deux WS. Couvrir d'abord par tests manuels et logs `mia.conv`.

## Mocking (proposé)

**`app/services/sms_service.py` et `telnyx_service.py`** :
- Mocker `urllib.request.urlopen` (pas de SDK officiel utilisé — appels HTTP
  bruts via stdlib).
- Vérifier l'URL, le payload JSON, les headers d'authentification.

**Base de données** : utiliser SQLite en mémoire dans `conftest.py` :
```python
@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
```
Note : le code production utilise JSONB (PostgreSQL). SQLite stocke en TEXT —
fonctionne pour la plupart des tests mais à valider pour `Commande.items`.
Alternative : Postgres via testcontainers.

**Settings** : override via monkeypatch :
```python
def test_xxx(monkeypatch):
    monkeypatch.setenv("MIA_API_KEY", "test-key")
    # recharger settings ou tester en isolant le module
```

**OpenAI Realtime** : ne pas tester l'intégration. Mocker la WS si vraiment
nécessaire avec `pytest-asyncio` + `unittest.mock.AsyncMock`.

## Fixtures et données de test

**Conventions à adopter :**
- Fixtures partagées dans `tests/conftest.py`.
- Factories simples (fonctions Python ordinaires) plutôt que `factory_boy` —
  cohérent avec la philosophie "moins de dépendances" du projet
  (cf. `app/config.py:1-9` qui justifie l'absence de `pydantic-settings`).

Exemple :
```python
def fabrique_restaurant(db, **overrides):
    r = Restaurant(nom="Chez Marco", telephone="+33692000000",
                   incoming_phone_number="+33800000001", **overrides)
    db.add(r); db.commit(); db.refresh(r)
    return r
```

## Coverage

**Requirements :** aucun seuil enforced.

**Recommandation :** viser ≥ 70 % sur `app/utils/`, `app/services/tool_service.py`,
`app/auth.py` avant de cibler le reste. Pas d'objectif de couverture globale
sur le bridge realtime (testé à la main).

## Test Types

**Unit Tests :** à privilégier pour `utils/`, helpers de `config.py`,
`tool_service.py` (logique pure et règles métier).

**Integration Tests :** `TestClient` FastAPI pour routes REST + dashboard —
exercice end-to-end avec DB SQLite mémoire.

**E2E Tests :** non applicables côté code. Tests bout-en-bout = appel réel
sur Telnyx en environnement de staging, avec un numéro dédié.

## Patterns spécifiques au domaine

**Tester les codes générés (`_code` dans `tool_service.py:30`) :**
- Vérifier longueur (préfixe + 4 chars).
- Vérifier l'alphabet (aucun caractère ambigu : pas de `0`, `1`, `O`, `I`,
  `L`, `S`, `5`, `B`, `8`).
- Vérifier l'unicité statistique (générer N codes, vérifier pas de doublon
  trivial — ne JAMAIS asserter de valeur fixe, c'est random).

**Tester les quotas** : créer N+1 réservations sur la même date, asserter
que la N+1 retourne `{"success": False, "recap_vocal": "..."}`.

**Tester `to_e164`** : table de cas (0612... → +33612..., +33612... → identité,
0692... → +33692... pour la Réunion, entrée invalide → None).

**Tester la signature HMAC du dashboard** : générer un token avec
`_sign_token`, vérifier qu'il est valide, le modifier d'un caractère, vérifier
qu'il est rejeté, simuler un timestamp ancien → expiré.

## Async Testing (si introduit)

```python
import pytest

@pytest.mark.asyncio
async def test_require_api_key_accepte_la_bonne_cle(monkeypatch):
    monkeypatch.setenv("MIA_API_KEY", "secret")
    # ...
```

Nécessite `pytest-asyncio` dans `requirements-dev.txt`.

---

*Testing analysis: 2026-05-13*
