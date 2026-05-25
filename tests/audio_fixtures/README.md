# Audio fixtures

Place ici des fichiers `.wav` **µ-law mono 8 kHz** capturés depuis de vrais
appels Telnyx. Ils alimentent les tests E2E qui rejouent l'audio dans le
bridge sans toucher OpenAI ni Telnyx (mocks).

## Comment capturer un wav réel

1. Sur le serveur Hetzner, active le dumper dans `.env` :
   ```bash
   AUDIO_DEBUG_DIR=/tmp/mia-audio
   sudo systemctl restart mia
   ```

2. Appelle MIA depuis ton téléphone, dis ce que tu veux capturer
   (« bonjour », « réservation 4 personnes demain 20h »...), raccroche.

3. Le bridge dump les 5 premières secondes dans `/tmp/mia-audio/call-<ts>-<cid>.wav`.

4. Récupère localement et renomme avec un nom parlant :
   ```bash
   scp mia@91.98.173.202:/tmp/mia-audio/call-*.wav .
   mv call-XXXX.wav tests/audio_fixtures/bonjour_simple.wav
   ```

5. Ajoute un test dans `tests/test_audio_e2e.py` qui rejoue ce wav et
   vérifie le comportement attendu.

## Convention de nommage

- `bonjour_*.wav` — salutations simples (pour vérifier que MIA ne dérive pas)
- `reservation_*.wav` — demandes de réservation complètes
- `commande_*.wav` — demandes de commande complètes
- `ambiguous_*.wav` — cas piégeux (silence, bruit, mots inaudibles)
- `confirmation_*.wav` — réponses "oui" / "non" / hésitations

## RGPD

Ces wavs contiennent la voix de vrais clients. **Ne pas commiter** si la voix
est identifiable (utilise ta propre voix pour les fixtures partagées). Le
`.gitignore` exclut `*.wav` par défaut dans ce dossier — voir le fichier
`.gitignore` à la racine.
