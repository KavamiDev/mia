# MIA — Suite de tests

## Lancer

```bash
# Setup local (une fois)
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements.txt

# Run all
.venv-test/bin/python -m pytest tests/ -v

# Run un fichier
.venv-test/bin/python -m pytest tests/test_sms.py -v

# Run un test précis
.venv-test/bin/python -m pytest tests/test_phone.py::test_e164_reunion_mobile_0692 -v
```

## Couverture

| Fichier | Couvre |
|---|---|
| `test_audio.py` | Parser RTP (RFC 3550) + AudioDumper WAV µ-law |
| `test_phone.py` | Normalisation E.164 (métropole + Réunion + Mayotte) + détection +262 |
| `test_sms.py` | Routage Telnyx/OVH + signature OVH + fallback silencieux |
| `test_client_state.py` | Sign/verify HMAC + détection altération + TTL anti-replay |
| `test_tools.py` | create_reservation, create_commande, quotas, codes, transfert |
| `test_api.py` | Routes REST + auth X-API-Key + webhook Telnyx |

**Total : 88 tests, < 1 seconde d'exécution.**

## Comment fonctionne le harnais

### DB SQLite in-memory (StaticPool)
Les tests qui touchent la DB utilisent SQLite en mémoire avec `StaticPool`
(une seule connexion partagée). Sans `StaticPool`, chaque ouverture de session
créerait une nouvelle DB vide → erreurs « no such table ».

### Patch JSONB → JSON
`app.models.Commande.items` utilise `postgresql.JSONB` qui ne marche pas sous
SQLite. `conftest.py` patch `postgresql.JSONB = JSON` AVANT l'import des
modèles.

### Patch SessionLocal global
`tool_service` et `voice_webhook` font `from app.database import SessionLocal`
au load → la référence est copiée. Patcher `database.SessionLocal` ne suffit
pas. La fixture patche SessionLocal dans TOUS les modules concernés.

### Variables d'environnement
Définies en tête de `conftest.py` AVANT tout import `app.*` car `settings`
est une dataclass figée lue une seule fois.

## Ajouter un test

Si tu touches au prompt MIA, à un router REST, ou au pipeline audio :
ajoute un test dans le fichier correspondant. La règle : **un bug fix =
un test qui aurait attrapé ce bug**.
