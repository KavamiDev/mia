# Migrations Alembic — MIA

Système de migrations DB. La base de vérité est `app/models.py` (SQLAlchemy).

## Commandes utiles

```bash
# Voir la version actuelle de la DB
alembic current

# Voir l'historique des migrations
alembic history

# Créer une nouvelle migration depuis les changements de models.py
alembic revision --autogenerate -m "ajout colonne X"

# Appliquer toutes les migrations en attente
alembic upgrade head

# Reculer d'une migration
alembic downgrade -1

# Générer le SQL d'une migration sans l'appliquer (review)
alembic upgrade head --sql
```

## Premier déploiement sur une nouvelle DB

```bash
# Crée toutes les tables depuis la migration baseline
alembic upgrade head
```

## Déploiement sur la DB de prod existante (tables déjà créées)

L'app a tourné avec `Base.metadata.create_all()` avant l'intro d'Alembic, donc
les tables existent déjà. Il faut **stamp** la prod à la version baseline
**sans rejouer** la migration (sinon erreur "table already exists") :

```bash
# Une seule fois, après le premier déploiement avec Alembic :
alembic stamp head
```

Pour les migrations **futures**, Alembic les appliquera normalement avec
`alembic upgrade head`.

## Workflow recommandé pour une modif de modèle

1. Modifier `app/models.py` (ajouter colonne, table, index, etc.)
2. `alembic revision --autogenerate -m "<description>"`
3. **Reviewer le fichier généré** dans `alembic/versions/` — autogenerate
   est imparfait, surtout pour : renames, contraintes complexes, JSONB.
4. Tester en local : `alembic upgrade head` puis `alembic downgrade -1`
5. Commit + déployer.

## Intégration avec create_all()

`app/main.py` continue d'appeler `Base.metadata.create_all()` au startup pour
les déploiements fraîchement clonés sans Alembic. Ça reste **idempotent** et
ne touche pas aux tables existantes. Alembic est la source de vérité pour
les **modifications** (ALTER TABLE), pas pour la création initiale.
