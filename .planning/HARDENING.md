# MIA — Phase Hardening (post-pilote)

> **Goal global** : éliminer les conditions qui forcent le restaurateur à faire un SAV
> avec nous. Si MIA se trompe, le restaurant veut annuler la commande/résa et nous
> appelle → coût opérationnel non-scalable.
>
> **SLOs cibles** :
> - Latence perçue : < 1.5 s entre fin de parole client et début de réponse MIA
> - Taux d'hallucination factuelle : 0 % (mesuré sur jeu de test de 20 appels reproductibles)
> - Taux de SAV restaurant : < 5 % des appels traités

---

## Quick wins déjà livrés (commit `012525b`)

| Fix | Statut | Impact |
|---|---|---|
| Prompt anti-hallucination | ✅ Déployé | « Bonjour » ne génère plus « table pour 4 » |
| Parser RTP RFC 3550 (CC+ext+pad) | ✅ Déployé + testé 6 cas | Plus de bytes parasites dans l'audio OpenAI |
| VAD réactif (700ms + 300ms) | ✅ Déployé | Latence 2.1s → 1.25s |
| Dumper WAV (env `AUDIO_DEBUG_DIR`) | ✅ Disponible | Validation manuelle du flux entrant |

**Action immédiate user** : appeler MIA, raccrocher, écouter le `.wav` généré. Si la voix est nette → audio OK, la prochaine couche c'est le prompt/modèle.

---

## Chantier 1 — Tests audio reproductibles 🧪

**Pourquoi** : aujourd'hui on teste MIA en l'appelant. Lent, non reproductible, biaisé par les conditions réseau. Un changement de prompt peut casser silencieusement la compréhension sur 30 % des inputs.

**Solution** : suite de tests qui rejoue des `.wav` réels contre une session Realtime mockée (ou contre l'API si on accepte le coût).

### Acceptance criteria
- [ ] `tests/audio_fixtures/` contient ≥ 10 wavs réels capturés (bonjour, résa simple, commande complexe, hésitations, transferts)
- [ ] `pytest tests/test_realtime_e2e.py` lance les fixtures et vérifie que MIA :
  - ne hallucine pas (transcription cohérente avec le wav)
  - appelle le bon tool (`create_reservation` / `create_commande` / aucun)
  - le récap contient les infos exactes du wav
- [ ] Test exécutable en < 60 s
- [ ] CI GitHub Actions qui bloque le merge si un test échoue

### Effort estimé
**1 journée** — capture des wavs (1h via dumper), setup pytest+fixtures (3h), mock OpenAI ou utilisation réelle avec budget cap (2h), CI (1h).

### Risque
Coût OpenAI Realtime si on tape l'API réelle (~0.30 $/min). Mitigation : 10 wavs × 30 s = 5 min = ~1.50 $/run. Acceptable.

---

## Chantier 2 — SMS fallback +262 via OVH SMS 📱

**Pourquoi** : Telnyx ne sait pas envoyer vers +262 (Réunion). Aujourd'hui le SMS de confirmation client échoue silencieusement → le client n'a pas la trace écrite de sa commande → SAV.

**Solution** : wrapper `sms_service.send_sms` qui détecte le pays et route vers OVH SMS pour +262.

### Acceptance criteria
- [ ] Compte OVH SMS créé (5 €/mois, ~1000 SMS inclus)
- [ ] `app/services/sms_service.py` ajoute `_send_via_ovh(to, body)` qui appelle l'API OVH SMS
- [ ] Logique : si `to.startswith("+262")` → OVH, sinon Telnyx
- [ ] Échec OVH → log + retour silencieux (comme Telnyx aujourd'hui)
- [ ] Variables d'env : `OVH_SMS_ACCOUNT`, `OVH_APPLICATION_KEY`, `OVH_APPLICATION_SECRET`, `OVH_CONSUMER_KEY`
- [ ] Test manuel : envoyer SMS à un +262 réel et vérifier réception

### Effort estimé
**Demi-journée** — création compte + auth OVH (1h), code wrapper (1h), test bout en bout (1h), docs config (30min).

### Risque
OVH SMS expéditeur : par défaut numérique français, on peut configurer un sender ID alphanumérique « MIA » mais limité à 11 chars et nécessite validation préalable côté OVH.

---

## Chantier 3 — Double-confirmation forte (anti-SAV) 🔒

**Pourquoi** : aujourd'hui MIA appelle `create_reservation` dès qu'elle pense avoir un "oui". Si le "oui" est ambigu (« euh oui mais... ») ou hallucination de transcription, la résa est créée à tort → SAV.

**Solution** : avant CHAQUE tool call de réservation/commande, exiger un récap explicite + un mot-clé de confirmation détecté dans la transcription suivante.

### Architecture proposée
1. Ajouter un état `pending_action` dans le bridge (`{"type": "reservation", "args": {...}}`)
2. Modifier le prompt : MIA dit « Je confirme [récap]. Vous dites OUI ou je modifie ? » et N'APPELLE PAS le tool encore
3. Côté bridge : à la prochaine transcription client, scan pour « oui / c'est ça / confirmé / valide » → exécution effective. Sinon → MIA demande clarification.

### Acceptance criteria
- [ ] Aucune `create_*` n'est jamais appelée sans un mot de confirmation détecté
- [ ] Si client dit « non » ou « attendez » → action annulée, MIA repropose
- [ ] Si client silencieux 5s après récap → MIA relance « j'attends votre confirmation »
- [ ] Mesurable : log d'évènements `confirmation_requested` / `confirmation_received` / `confirmation_rejected`

### Effort estimé
**1 journée** — refacto du flow tool dans `realtime_service.py` (4h), tests E2E (3h), réglages prompt (1h).

### Risque
Ajoute 1 tour de parole = +1.5 s à l'appel. Compensation : le SAV évité vaut largement la perte. Pour les commandes simples (< 20 €), on peut keep le flow actuel ; double-confirm uniquement au-dessus d'un seuil.

---

## Chantier 4 — Dashboard SAV (logs structurés) 📊

**Pourquoi** : quand le restaurateur nous appelle pour un SAV (« la résa de jeudi soir est fausse »), on n'a pas d'outil pour revoir ce qui s'est passé. Aujourd'hui les transcripts sont dans les logs serveur, non triables, non corrélables.

**Solution** : table `call_log` + page dashboard qui affiche par appel : transcript complet (client + MIA), tools appelés, durée, code de résa/commande créé.

### Acceptance criteria
- [ ] Table `call_log(id, restaurant_id, caller_phone, started_at, ended_at, transcript_json, tool_calls_json)`
- [ ] `realtime_service` accumule les évènements `conversation.item.input_audio_transcription.completed` et `response.output_audio_transcript.done` dans un buffer, flush en DB à la fin de l'appel
- [ ] Page `/dashboard/calls?restaurant=X` qui liste les 50 derniers appels avec filtre date
- [ ] Page `/dashboard/calls/<id>` qui affiche le transcript ligne par ligne avec horodatages
- [ ] Lien depuis chaque résa/commande vers l'appel d'origine
- [ ] Bouton « marquer comme SAV » avec champ note libre

### Effort estimé
**1.5 jour** — modèle + migration (2h), buffer dans bridge (2h), templates dashboard (4h), filtres + recherche (3h), tests (1h).

### Risque RGPD
Les transcripts contiennent des données personnelles (commande nominative, etc.). Mitigation : rétention 30 jours auto-purge + accès admin uniquement + mention dans le greeting MIA (« cet appel peut être enregistré pour amélioration du service »).

---

## Ordre d'exécution recommandé

```
1. Chantier 1 (tests) → bloque les régressions sur la suite
2. Chantier 2 (SMS OVH) → débloque le SAV silencieux sur +262
3. Chantier 4 (dashboard SAV) → outil pour MESURER le taux de SAV
4. Chantier 3 (double-confirm) → réduit le taux de SAV mesuré
```

**Pourquoi cet ordre** : sans tests on régresse à chaque commit. Sans SMS on perd des clients silencieusement. Sans dashboard on ne sait pas combien de SAV on a. Double-confirm en dernier car c'est le plus impactant sur la latence — à ajuster au vu des métriques du dashboard.

**Total** : ~4 jours-ingé. Réaliste en 1 sprint d'une semaine.

---

## Pour exécuter un chantier en mode GSD complet

Une fois un chantier choisi, lance :

```bash
/gsd-plan-phase
# Puis dans le menu : choisis le chantier
# Suivi de :
/gsd-execute-phase
```

Cela générera un PLAN.md détaillé (tasks atomiques, dépendances, critères de validation) et l'exécutera commit par commit avec verification automatique.
