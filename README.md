# MIA - Assistante téléphonique restaurant

MIA répond aux appels des clients, prend les réservations et les commandes à emporter.
Le restaurateur reçoit un SMS avec les détails.

## Fonctionnement

1. Le client appelle le numéro du restaurant (Telnyx)
2. MIA répond naturellement, connaît le menu et les horaires
3. Elle prend la réservation ou la commande
4. Le restaurateur reçoit un SMS :

```
Réservation :
4 personnes
20h30 le 2026-04-05
Nom : Dupont
Tel : 0692XXXXXX
```

```
Commande à emporter :
2 Pizza Reine, 1 Coca
Total : 29.00€
Tel : 0692XXXXXX
```

## Stack

- **Backend** : FastAPI (Python)
- **Voix** : OpenAI Realtime API (gpt-4o-mini-realtime)
- **Téléphonie** : Telnyx (appels + SMS)
- **Base de données** : PostgreSQL

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env   # Remplir les clés
```

### Base de données

```bash
psql -d mia -f scripts/init_db.sql
```

### Créer un restaurant

```bash
curl -X POST http://localhost:8000/restaurants \
  -H "Content-Type: application/json" \
  -d '{
    "nom": "Chez Marco",
    "telephone": "+33692XXXXXX",
    "horaires": "Lun-Sam 12h-14h, 19h-22h",
    "incoming_phone_number": "+33XXXXXXXXX",
    "quota_reservations": 20,
    "quota_commandes": 30
  }'
```

### Ajouter le menu

```bash
curl -X POST http://localhost:8000/menu/restaurant/1/bulk \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"nom_plat": "Pizza Margherita", "prix": 12.00},
      {"nom_plat": "Pizza Reine", "prix": 14.00},
      {"nom_plat": "Coca-Cola", "prix": 3.00}
    ]
  }'
```

### Lancer

```bash
uvicorn app.main:app --reload
```

### Webhook Telnyx

Configurer le numéro Telnyx pour envoyer les webhooks vers :
```
https://votre-domaine.com/voice/incoming
```

## API

| Endpoint | Description |
|---|---|
| `GET /` | Health check |
| `GET /restaurants` | Liste des restaurants |
| `POST /restaurants` | Créer un restaurant |
| `GET /reservations` | Liste des réservations |
| `GET /commandes` | Liste des commandes |
| `GET /menu/restaurant/{id}` | Menu d'un restaurant |
| `POST /menu/restaurant/{id}/bulk` | Ajouter le menu |
| `POST /voice/incoming` | Webhook Telnyx (appels) |
| `WS /voice/media-stream` | Stream audio Telnyx ↔ OpenAI |
