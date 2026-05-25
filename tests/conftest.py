"""Fixtures partagées pour tous les tests MIA.

⚠ Ordre critique : on configure l'environnement de test AVANT tout import
de `app.*` car :

  1. `app.config.Settings` est une dataclass figée lue UNE SEULE FOIS au
     chargement du module → les env vars doivent exister avant.

  2. Les modèles utilisent `sqlalchemy.dialects.postgresql.JSONB` qui ne
     fonctionne pas sous SQLite → on swap JSONB → JSON générique avant
     l'import des modèles.
"""
import os

# ───── 1. Variables d'environnement de test (avant tout import app.*) ─────
# DB : SQLite in-memory partagée entre les sessions (cache=shared).
# DATABASE_URL doit être valide pour que create_engine() ne pète pas à l'import.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Clé API REST requise par require_api_key.
os.environ.setdefault("MIA_API_KEY", "test-api-key-32-bytes-of-random")
# Secrets dashboard (sinon main.py crash en startup_check si looks_like_production).
os.environ.setdefault("DASHBOARD_PASSWORD", "test-password-not-default")
os.environ.setdefault("DASHBOARD_SECRET", "test-secret-32-bytes-minimum-for-hmac")
# OpenAI/Telnyx : on met des valeurs bidon, les tests qui les utilisent mockent urllib.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake")
os.environ.setdefault("TELNYX_API_KEY", "KEY_TEST_FAKE")
os.environ.setdefault("TELNYX_PHONE_NUMBER", "+33612345678")
# OVH SMS : config complète bidon pour pouvoir tester le routage.
os.environ.setdefault("OVH_APPLICATION_KEY", "ovh-app-key-test")
os.environ.setdefault("OVH_APPLICATION_SECRET", "ovh-app-secret-test")
os.environ.setdefault("OVH_CONSUMER_KEY", "ovh-consumer-key-test")
os.environ.setdefault("OVH_SMS_ACCOUNT", "sms-test-1")
os.environ.setdefault("BACKEND_URL", "http://localhost:8000")

# ───── 2. Patch JSONB → JSON pour SQLite ─────
# JSONB est postgres-only. SQLite ne le comprend pas. On force le swap au
# niveau du module dialects.postgresql AVANT que models.py l'importe.
from sqlalchemy import JSON  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

postgresql.JSONB = JSON  # type: ignore[assignment]

# ───── 3. Imports app.* (maintenant sûrs) ─────
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import database  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


# ───── 4. Fixtures ─────


@pytest.fixture(scope="function")
def db_engine():
    """Engine SQLite in-memory frais pour chaque test (isolation totale).

    StaticPool : SQLite ':memory:' crée normalement une NOUVELLE base par
    connexion. Avec StaticPool, on partage UNE seule connexion entre toutes
    les sessions ouvertes — sinon les tables créées ici ne seraient pas
    visibles depuis les requêtes FastAPI (qui ouvrent leurs propres sessions).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _patch_session_local(new_session_local):
    """Patch SessionLocal dans TOUS les modules qui l'ont importé directement.

    `from app.database import SessionLocal` copie la référence au moment de
    l'import → patcher `database.SessionLocal` ne suffit pas. On doit
    patcher chaque module qui a fait l'import direct.

    Returns:
        Liste de (module, original) pour pouvoir restaurer après le test.
    """
    from app.routers import voice_webhook
    from app.services import call_log_service, tool_service

    targets = [database, tool_service, voice_webhook, call_log_service]
    originals = []
    for mod in targets:
        if hasattr(mod, "SessionLocal"):
            originals.append((mod, mod.SessionLocal))
            mod.SessionLocal = new_session_local
    return originals


def _restore_session_local(originals):
    for mod, sess in originals:
        mod.SessionLocal = sess


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Session SQLAlchemy utilisable directement dans un test (sans HTTP).

    Patche aussi le SessionLocal global → les tools (qui n'utilisent pas DI)
    voient bien la DB de test.
    """
    TestSessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    originals = _patch_session_local(TestSessionLocal)
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        _restore_session_local(originals)


@pytest.fixture(scope="function")
def client(db_engine):
    """TestClient FastAPI avec DB in-memory et auth API activée.

    Override la dépendance get_db pour utiliser notre engine de test.
    Patche SessionLocal partout pour que tool_service voie aussi la DB de test.
    """
    TestSessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    originals = _patch_session_local(TestSessionLocal)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        _restore_session_local(originals)


@pytest.fixture
def auth_headers():
    """Headers avec API key valide pour les routes protégées."""
    return {"X-API-Key": os.environ["MIA_API_KEY"]}


@pytest.fixture
def sample_restaurant(db_session):
    """Crée un restaurant de test directement en DB.

    Utile pour les tests de tool_service qui ont besoin d'un restaurant
    existant sans passer par l'API.
    """
    from app.models import MenuItem, Restaurant

    r = Restaurant(
        nom="Chez Marco",
        telephone="+33612345678",
        adresse="1 rue de Test",
        horaires="11h-14h, 19h-22h",
        incoming_phone_number="+33645678900",
        quota_reservations=20,
        quota_commandes=50,
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)

    # Quelques plats pour tester create_commande.
    for nom, prix in [("Pizza Margherita", 12.0), ("Pizza Reine", 14.0),
                       ("Tiramisu", 6.50), ("Coca", 3.0)]:
        db_session.add(MenuItem(restaurant_id=r.id, nom_plat=nom, prix=prix))
    db_session.commit()
    return r
