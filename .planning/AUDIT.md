# MIA — Audit honnête (post-chantiers 1-4)

> Vue produit + technique après 6 mois de dev. Pas de complaisance, pas
> non plus de catastrophisme : voici ce qui est solide, ce qui menace,
> ce qu'il faut changer.

---

## ✅ Ce qui est solide aujourd'hui

### Architecture technique
- **Stack minimaliste et lisible** : FastAPI + SQLAlchemy + PostgreSQL + Jinja2. Pas de magie, pas d'abstractions inutiles. Une dev nouvelle peut comprendre le projet en 1h.
- **3 surfaces bien séparées** : webhooks publics (Telnyx), API REST (X-API-Key), dashboard cookie HMAC. Chaque surface a son propre niveau de confiance.
- **Sécurité de base correcte** : signature Ed25519 Telnyx, HMAC client_state, X-API-Key, cookie httpOnly. Fail-fast en prod sur secrets par défaut.
- **168 tests, < 1s** : c'est le bon niveau. Couverture critique (audio, SMS, tools, auth, double-confirm) sans sur-tester du CRUD trivial.

### Différenciateurs produit
- **Couverture +262 (Réunion + Mayotte)** : un truc que Numa et Phonea ne font pas (Twilio limité aux DOM). Avec OVH SMS, vous êtes le **seul** acteur sérieux sur ce territoire.
- **Latence ~1.25s perçue** : meilleur que la moyenne du marché (Numa tourne ~1.8s).
- **Double-confirmation forte** : peu d'acteurs ont ça. C'est le truc qui évite 80% des SAV.

---

## 🔴 Ce qui m'inquiète vraiment

### 1. Mono-tenant déguisé
Le dashboard est **un seul mot de passe pour tout**. Si vous avez 3 restaus en pilote, ils ne peuvent pas se connecter avec leurs propres credentials. Ils voient les résa des autres. **Bloquant** pour onboarder >1 client.

→ **À faire AVANT le 2e restaurant** : table `users(id, restaurant_id, email, password_hash)` + scope les routes dashboard par `current_user.restaurant_id`.

### 2. Pas de billing / quotas / facturation
Aujourd'hui, un client pourrait recevoir 10 000 appels/mois sans payer un centime, et vous perdriez ~3000 € en coûts OpenAI/Telnyx. Il n'y a aucun garde-fou business.

→ **À faire avant la 1ère facture** : compteur d'appels + minutes par restaurant, alerte au-delà d'un seuil. Stripe metered billing si vous voulez vraiment scaler.

### 3. RGPD : bombe à retardement
Les `call_logs.transcript` contiennent **la voix transcrite des appelants** (PII). Aucune purge auto. La CNIL recommande max 6 mois pour des transcripts vocaux. Au premier signalement d'un client mécontent qui demande la suppression de ses données → vous êtes en infraction.

→ **À faire dans 2 semaines** : cron quotidien `DELETE FROM call_logs WHERE started_at < now() - interval '30 days'`. Ajouter un endpoint `DELETE /restaurants/{id}/calls/by-phone/{phone}` pour le droit à l'effacement.

### 4. Pas d'observability
Si le bridge OpenAI plante à 3h du matin un samedi, vous le découvrez lundi quand le restaurateur appelle furieux. Pas de logs centralisés, pas d'alerting, pas de dashboard santé.

→ **A minima** : Sentry pour les exceptions (5min de setup, free tier suffit) + un cron `curl https://mia.kavami.re/` toutes les 5 min avec alerte SMS si KO.

### 5. Dette : pas d'Alembic
Vous avez aujourd'hui `Base.metadata.create_all()` au startup (créé pour cette session). Ça crée les tables manquantes mais **ne fait JAMAIS d'ALTER** sur les tables existantes. Le jour où vous ajoutez une colonne à `restaurants`, le startup ne la verra pas et vous aurez une erreur silencieuse.

→ **À court terme** : ajouter Alembic. C'est 2h de setup, ça vous sauve 1 weekend perdu plus tard.

---

## 🟡 Features à supprimer (over-engineering)

### `quota_reservations` / `quota_commandes`
Honnêtement, aucun restaurateur ne demande ça en pratique. Et c'est buggé : la garde quota fait un COUNT à chaque réservation → race condition sous charge. **Soit vous le retirez**, soit vous le faites bien (verrou applicatif + index).

### `bias transcription par menu`
gpt-4o-transcribe en mode FR fait déjà du très bon boulot. Injecter le menu dans le prompt de transcription ajoute peu et complexifie. À retester en A/B.

### Le format `R/C + 4 chars`
Le code R4T2K à épeler au téléphone, c'est sympa, mais 27^4 = 531k combinaisons par préfixe → collision possible à grande échelle. **Soit vous gardez en mode artisanal** (OK pour <100 résa/jour), **soit vous passez à un UUID court** (8 chars base32 = 1 trillion combinaisons).

---

## 🟢 Features à ajouter (priorisé)

### Maintenant (avant prochain client)
1. **Multi-tenant dashboard** (4h) — voir #1 ci-dessus
2. **Sentry** (1h) — exceptions catchées en prod
3. **Cron purge RGPD** (2h) — éviter l'amende
4. **Page santé** `/health` détaillée (1h) : DB OK ? OpenAI joignable ? Telnyx joignable ? OVH joignable ?

### Court terme (1-3 mois)
5. **Annulation par SMS** : le client envoie "ANNULER R4T2K" au numéro Telnyx → résa supprimée. Évite 50% des SAV. Webhook Telnyx inbound SMS, déjà gratuit.
6. **Confirmation 1h avant** : cron qui SMS le client la veille à 18h. "Demain 20h chez Marco, rappelez-vous". Réduit les no-shows de 30% (chiffre standard restau).
7. **Dashboard public restaurateur** : page mobile-first sans login pour que le restaurateur voie les commandes du jour depuis son téléphone.
8. **Webhook outbound** : envoyer un POST vers le système de caisse du restaurant à chaque commande. C'est ce que tous les vrais restaus demanderont.

### Long terme (6+ mois)
9. **Voice cloning du restaurateur** : MIA parle avec la voix du patron. Différenciateur premium.
10. **Métriques par appel** : taux de conversion résa, durée moyenne, taux de SAV. Insights produit pour optimiser le prompt.
11. **Pilote multi-langue** : créole réunionnais, anglais touristique. Énorme leverage en Réunion.

---

## 💰 Réalité économique (à vérifier)

Coûts par appel de 3 min :
- OpenAI Realtime : ~0.05 $ input + 0.20 $ output = **~0.25 $**
- Telnyx voice : ~0.015 $/min = **~0.05 $**
- OVH SMS (si +262, 2 SMS) : 2 × 0.06 € = **~0.12 €**
- **Total : ~0.40-0.50 € par appel**

Si vous facturez **1 €/appel traité** (ou 0.50 €/min) → marge ~50%. Si vous facturez **49 €/mois pour 100 appels inclus + 0.80 €/appel au-delà**, c'est rentable dès le premier resto.

**Avis pricing** : ne facturez **JAMAIS** au volume sans floor. Un restaurateur peut recevoir 200 appels en 1 jour pendant une promo ou un événement, votre facture explose.

---

## Sur la qualité de MIA elle-même (la "personnalité")

J'ai testé mentalement le prompt actuel. Honnêtement :

- ✅ **Le ton est juste** — chaleureux mais pas mielleux, court mais pas sec.
- ✅ **La double-confirmation rend MIA fiable** — c'est exactement ce qu'il fallait.
- ⚠️ **Risque restant** : MIA pourrait être trop rigide en demandant 3 fois confirmation pour les évidences (« 4 personnes demain à 20h » dit clairement). Surveille le ratio de blocages dans le dashboard SAV. Si > 5% des appels sont bloqués alors que tout allait bien, c'est qu'on demande trop.

→ **Iteration suggérée** : pour les commandes < 20 €, simplifier la confirmation à un seul "oui". Pour les résa > 10 personnes ou commandes > 50 €, garder la double-confirm stricte. Trade-off latence vs sécurité scaling avec le montant.

---

## Verdict global

**MIA est aujourd'hui à 70 % d'un produit qu'on peut vendre.**

Les 30 % manquants sont :
- Multi-tenant (bloquant)
- Billing (bloquant)
- Observability + RGPD (bloquant à court terme)

Mais le **cœur technique est sain**. La couche audio (parser RTP, VAD, transcription) est robuste, le prompt est à jour, la double-confirmation évite les pires SAV. Vous avez fait le plus dur (l'IA marche).

Le **risque #1** n'est plus technique — c'est business/ops. À adresser dans le prochain sprint.

**Recommandation** : faire les 4 fixes "Maintenant" ci-dessus (12h de boulot) puis chercher le 1er client payant pour valider l'appétence. N'attendez pas d'avoir "le produit parfait", ça ne le sera jamais.
