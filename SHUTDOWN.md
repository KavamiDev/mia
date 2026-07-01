# Décommissionnement de MIA

Runbook pour arrêter proprement MIA et couper toute facturation.
Ordre voulu : **sauvegarder → Telnyx → Hetzner → DNS → vérif finale.**

> État du code au moment d'écrire ce runbook : **tout est sur GitHub.**
> Backend FastAPI (`app/`), config (`app/config.py`), prompt/logique voix
> (`prompts/mia_system_prompt.md`), templates dashboard, migrations Alembic et
> units systemd de purge sont versionnés. Détruire le VPS ne perd **aucun code**.
> Ce qui n'est PAS dans le dépôt : le `.env` de prod et la base Postgres (locale
> sur le VPS). C'est tout ce qu'il faut rapatrier avant de casser.

---

## 1. Sauvegarder avant de casser

### 1a. Code — déjà fait
Rien à rapatrier côté code : le dépôt est à jour. Le `.env` réel n'est
volontairement pas versionné (`.gitignore`).

### 1b. Rapatrier les secrets du VPS
Les valeurs de prod ne vivent que dans `/opt/mia/.env`. Récupère-les avant de
supprimer le serveur (garde-les dans un gestionnaire de mots de passe, pas en
clair) :

```bash
ssh <user>@<vps> "cat /opt/mia/.env"
# ou copie le fichier :
scp <user>@<vps>:/opt/mia/.env ./mia-prod.env.backup
```

Clés à ne pas perdre (cf. `.env.example`) :
- `OPENAI_API_KEY` — révoquer sur platform.openai.com si MIA est le seul usage.
- `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` — deviendront inutiles après l'étape 2.
- `BREVO_API_KEY` — SMS +262. Révoquer sur app.brevo.com si plus utilisé.
- `MIA_API_KEY`, `DASHBOARD_PASSWORD`, `DASHBOARD_SECRET` — secrets internes.
- `SENTRY_DSN` — si un projet Sentry existe, pense à le fermer aussi.

> Sécurité : une fois le service mort, **révoque** les clés API réutilisables
> (OpenAI, Brevo) plutôt que de juste les archiver. Une clé qui traîne = risque.

### 1c. Dumper la base Postgres
La base MIA est un **Postgres local sur le VPS** (`DATABASE_URL=...localhost:5432/mia`),
pas Supabase. Elle disparaît avec le serveur → dump avant suppression :

```bash
ssh <user>@<vps> "pg_dump -Fc mia" > mia-db-$(date +%Y%m%d).dump
# restauration ultérieure éventuelle :
#   pg_restore -d mia mia-db-YYYYMMDD.dump
```

### 1d. Prompts / logique voix
Déjà versionnés dans `prompts/mia_system_prompt.md` et `app/services/realtime_service.py`.
Rien à noter à part.

### 1e. (Optionnel) Arrêter le service proprement
Pour ne pas couper un appel en cours brutalement :

```bash
ssh <user>@<vps>
sudo systemctl disable --now mia-purge.timer
sudo systemctl stop mia           # nom réel du service applicatif à confirmer
```

---

## 2. Telnyx (facture au numéro — à résilier en premier)

- **Numbers** → sélectionner le numéro → **Release**.
  ⚠️ Un numéro relâché est **perdu définitivement**. Sûr de ne pas le vouloir.
- Supprimer les **Voice API Applications / Call Control apps** liées.
- Supprimer les **Outbound Voice Profiles** et les **Connections**.
- **Billing** → vérifier le solde, **couper l'auto-recharge** pour ne pas laisser
  un fond de facturation actif.

---

## 3. Hetzner (le gros poste mensuel)

- **Cloud Console** → le projet → **Server** → **Delete**.
- Vérifier et supprimer ce qui facture même sans serveur :
  - **Volumes**
  - **Floating IPs**
  - **Snapshots / Backups**
  - **Load Balancers** (si présents)
- Une fois le projet vide, **supprimer le projet** lui-même.

---

## 4. DNS

- Chez le registrar : supprimer les enregistrements **A / CNAME** qui pointaient
  vers le VPS (le domaine de `BACKEND_URL` / `VOICE_REALTIME_DOMAIN`).
- Domaine : le garder ou le laisser expirer ?
  Si le nom ne sert plus, **couper le renouvellement auto** pour ne pas repayer
  une année.

---

## 5. Vérification finale (le lendemain / la semaine d'après)

Repasser sur chaque dashboard de facturation pour confirmer qu'il ne reste rien
d'actif :
- [ ] Telnyx — numéro relâché, auto-recharge coupée, solde OK.
- [ ] Hetzner — serveur + volumes + IPs + snapshots supprimés, projet vide/supprimé.
- [ ] Registrar — enregistrements DNS supprimés, renouvellement auto décidé.
- [ ] OpenAI — clé révoquée si MIA était le seul usage.
- [ ] Brevo — clé révoquée si plus de SMS.
- [ ] Sentry — projet fermé si présent.
