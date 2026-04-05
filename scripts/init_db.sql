-- ──────────────────────────────────────
-- MIA — Initialisation de la base de données
-- ──────────────────────────────────────
-- Usage : psql -U mia -d mia -f scripts/init_db.sql

-- Fiche restaurant : chaque restaurant a un numéro Telnyx dédié
-- et des quotas optionnels pour limiter les réservations/commandes par jour.
CREATE TABLE IF NOT EXISTS restaurants (
    id SERIAL PRIMARY KEY,
    nom VARCHAR(255) NOT NULL,
    telephone VARCHAR(20) NOT NULL,        -- Numéro du restaurateur (reçoit les SMS)
    adresse TEXT,
    horaires TEXT,
    incoming_phone_number VARCHAR(20) UNIQUE, -- Numéro Telnyx assigné (routage des appels)
    quota_reservations INTEGER,             -- NULL = illimité
    quota_commandes INTEGER,                -- NULL = illimité
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Réservations créées par MIA pendant les appels.
-- Le code (ex: R4T2K) est communiqué vocalement au client et par SMS.
CREATE TABLE IF NOT EXISTS reservations (
    id SERIAL PRIMARY KEY,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    code VARCHAR(10) NOT NULL,
    personnes INTEGER NOT NULL,
    heure VARCHAR(10) NOT NULL,
    telephone VARCHAR(20),                  -- Numéro de l'appelant (auto-détecté)
    date VARCHAR(10) NOT NULL,              -- Format AAAA-MM-JJ
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Commandes à emporter créées par MIA.
-- Les items sont stockés en JSONB [{plat, qty, prix_unitaire}].
CREATE TABLE IF NOT EXISTS commandes (
    id SERIAL PRIMARY KEY,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    code VARCHAR(10) NOT NULL,
    items JSONB NOT NULL,
    prix_total FLOAT NOT NULL DEFAULT 0,
    telephone VARCHAR(20),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Menu du restaurant : MIA utilise ces données pour répondre aux
-- questions sur les plats et calculer les totaux des commandes.
CREATE TABLE IF NOT EXISTS menu (
    id SERIAL PRIMARY KEY,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    nom_plat VARCHAR(255) NOT NULL,
    prix FLOAT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index pour les requêtes fréquentes
CREATE INDEX IF NOT EXISTS idx_reservations_restaurant_id ON reservations(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_reservations_date ON reservations(date);
CREATE INDEX IF NOT EXISTS idx_reservations_code ON reservations(code);
CREATE INDEX IF NOT EXISTS idx_commandes_restaurant_id ON commandes(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_commandes_code ON commandes(code);
CREATE INDEX IF NOT EXISTS idx_menu_restaurant_id ON menu(restaurant_id);
