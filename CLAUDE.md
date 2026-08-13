# Projet : publication automatisée des recaps TFT (GitHub Pages + Discord)

## État du code

Python 3.10+, deux modules, aucune dépendance à un serveur.

| fichier | rôle |
|---|---|
| `metatft.py` | export MetaTFT + analyse + génération du site statique |
| `publish.py` | diff entre deux générations, déploiement `docs/`, annonces Discord |
| `sets.json` | profils de set (`--set 18-pbe` → `queue=PBE`) |
| `comps.json`, `comps.<set>.json` | comps à analyser (URL explorer MetaTFT) |
| `comp_channels.json` | routage comp → channel Discord (noms de variables, pas d'URL) |
| `.github/workflows/recap.yml` | cron quotidien 06:00 UTC + `workflow_dispatch` |

### Ce que produit une génération

`python metatft.py --output <dir>` écrit dans `<dir>` :

- `index.html` — tableau de toutes les comps (`games`, `share_of_meta`, `avg_place`,
  `d_avg_vs_meta`, `top4`, `win`), liens **URL-encodés** vers chaque report ;
- `summary.csv` — mêmes colonnes, plus `champ` et `erreur`. C'est **la seule
  entrée de `publish.py`** ;
- `<Comp>/report.html` — report détaillé, un dossier par comp ;
- `<Comp>/{builds,item_lift,paires,items_*,etoiles,unites_flex,traits_flex}.csv`
  et les PNG correspondants, plus `data.json`.

Le nom de dossier vient de `comp_folder(name, champ)` : c'est `name`, suffixé de
`- <carry>` si plusieurs comps partagent le même nom. Il contient espaces et
accents, d'où l'encodage des liens.

## Pipeline de publication

```
metatft.py --publish [--deploy] [--notify] [--publish-dry-run]
        │
        └── publish.publish(site_dir, …)
              1. read_summary(site_dir)            → état courant
              2. load_snapshot(data/last_run.json) → état précédent
              3. diff_runs(…, threshold=0.10)      → list[Change]
              4. deploy()   : site → docs/ + commit (+ push)
              5. notify     : embed général + routage par channel de comp
              6. save_snapshot()
```

`publish.py` s'utilise aussi seul sur un dossier déjà généré :

```bash
python publish.py --site output_set18_pbe --dry-run            # simulation
python publish.py --site site --deploy --notify                # réel
```

### Règles de non-spam

- Seuil : un mouvement compte si `abs(avg_place - précédent) >= --threshold`
  (0.10 place par défaut). En dessous, c'est du bruit d'échantillonnage.
- Aucun changement → ni déploiement ni notification (sauf `--force`).
- **Première exécution** (pas de snapshot) : le site est déployé, le snapshot est
  écrit, mais rien n'est posté — sinon 20 « nouvelles entrées » d'un coup.
- Une comp disparue n'est pas signalée : elle passe souvent juste sous
  `--min-games` d'un jour à l'autre.

### Hébergement

GitHub Pages sur `main` + dossier `/docs`. `deploy()` copie le dossier généré
dans `docs/`, y ajoute `.nojekyll`, commit et push. Pas de branche orpheline :
le site suit le même historique que le code.

## Secrets

Jamais dans le dépôt. `.env` local (ignoré par git), secrets GitHub en CI.
`comp_channels.json` ne stocke que le **nom** de la variable (`webhook_env`).

| variable | usage |
|---|---|
| `SITE_URL` | base des liens dans les embeds |
| `DISCORD_WEBHOOK_GENERAL` | channel général |
| `DISCORD_WEBHOOK_<SLUG>` | channel de comp, cf. `comp_channels.json` |

Le push CI utilise le `GITHUB_TOKEN` fourni par l'Action (`permissions: contents: write`).

## Reste à faire (nécessite des accès que le code ne peut pas obtenir seul)

1. `git init`, premier commit, créer le dépôt distant, `git remote add origin …`.
2. Repo → Settings → Pages → Source : `main` / `/docs`.
3. Créer les webhooks Discord et les enregistrer en secrets GitHub ; renseigner
   `SITE_URL` en variable de dépôt.
4. Remplir `comp_channels.json` avec les vrais channels : les entrées actuelles
   (`blackthorn`, `juggernaut`, `primal`) sont des exemples construits sur les
   traits du Set 18, à remplacer par l'organisation réelle du serveur.
5. Premier `--deploy` manuel pour vérifier les liens en ligne, puis laisser le cron.

## Conventions

- Réutiliser le pipeline existant, ne pas le réécrire : la publication est une
  étape terminale, jamais une transformation des données d'analyse.
- Toute action sortante (push, webhook) doit rester derrière un flag explicite et
  supporter `--dry-run`.
- Les fichiers de config JSON sont lus en `utf-8-sig` (édition sous Windows).
