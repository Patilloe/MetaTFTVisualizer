# metatft — analyse des comps via l'API MetaTFT Explorer

## Installation

```bash
pip install requests matplotlib pillow
```

## Utilisation

```bash
python metatft.py                       # set live (queue ranked), comps.json
python metatft.py --set 18-pbe          # Set 18 en test sur le PBE
python metatft.py --set 18-pbe --discover 20   # genere comps.18-pbe.json
python metatft.py --set 18-pbe --validate      # comps compatibles avec le Set 18 ?
python metatft.py --days 3 --ranks CHALLENGER,GRANDMASTER --min-games 80
```

Options utiles : `--min-games` (echantillon minimum par ligne), `--prior` (force du
lissage bayesien), `--alpha` (seuil FDR), `--scatter`, `--no-plots`, `--no-icons`,
`--no-html`, `--workers`, `--cache-ttl 0` (desactive le cache).

## Ce qui est produit, par comp

| fichier | contenu |
|---|---|
| `builds.csv/.png` | builds complets (3 items) du carry |
| `builds_partiels.csv` | builds a 1-2 items (early / unites secondaires) |
| `item_lift.csv/.png` | **lift marginal par item** : toutes les parties contenant l'item |
| `paires.csv` | lift des paires d'items (cores a 2 items) |
| `items_radiant / artifact / mechanic / emblem / craftable` | items uniques par categorie |
| `etoiles.csv/.png` | performance du carry en 1\*/2\*/3\* |
| `unites_flex.csv` | unites qui, ajoutees a la comp, changent le resultat |
| `traits_flex.csv` | idem pour les traits |
| `data.json` | tout, brut |
| `report.html` | rapport lisible; `output/index.html` regroupe les comps |

## Comment la taille d'echantillon est prise en compte

Une place moyenne brute n'est pas comparable entre un build joue dans 50 % des
parties et un build joue dans 1 % : le second est **selectionne** (on ne le joue
que quand les conditions s'y pretent, souvent par des joueurs precis), donc sa
moyenne est optimiste, en plus d'etre incertaine.

Le classement utilise donc `score`, la difference **lissee** vers la moyenne de
la comp, avec un prior proportionnel a l'echantillon :

```
prior      = max(--prior, --prior-frac x parties de la comp)     # defaut 2 %
shrunk_avg = (n x avg + prior x avg_comp) / (n + prior)
score      = shrunk_avg - avg_comp
```

Concretement, sur une comp a 83 000 parties (prior = 1 667) :

| build | n | part | avg brut | shrunk | rang |
|---|---|---|---|---|---|
| Emblem Blackthorn + Blue Buff + Jeweled Gauntlet | 2 627 | 3.2 % | 3.42 | **3.57** | 1er |
| Emblem Executioner + Blue Buff + Rabadons | 142 | 0.2 % | 2.60 | 3.72 | 13e |

Le second a beau afficher 0.8 place de mieux en brut, il ne remonte pas : son
avantage n'est pas suffisamment atteste par le volume.

Trois colonnes completent la lecture :

- `d_avg_pess` : borne la moins flatteuse de l'IC 95 % — ce que le build peut
  **prouver**. Un tri sur cette colonne ne remonte que ce qui est solide.
- `impact` : `-score x share`, le gain de places apporte a la comp entiere.
  Repond a « ou est-ce que je gagne le plus de points au total ».
- `share` : part des parties de la comp.

## Colonnes des CSV

- `n` : parties ; `share` : part des parties de la comp
- `avg_place` : place moyenne brute ; `shrunk_avg` : lissee (cf. ci-dessus)
- `score` : `shrunk_avg - moyenne de la comp` → **colonne de tri**
- `d_avg`, `d_avg_pess`, `impact`, `d_top4`, `d_win` : cf. ci-dessus
- `z`, `p`, `significant` : test de difference des moyennes, `significant`
  applique la correction Benjamini-Hochberg (FDR) sur les lignes du tableau
- `group` : `populaire` (top joue) ou `pepite` (peu joue mais meilleur)

Sur les graphiques : longueur de barre = delta brut, **epaisseur de barre = part
de l'echantillon**, losange = valeur lissee qui sert au classement, moustaches =
IC 95 %, `*` = significatif apres correction FDR.

## Sets

Un set ne se designe pas par un prefixe mais par un couple **queue + patch** :

| `--set` | queue | contenu |
|---|---|---|
| *(absent)* | `1100` | saison live en ranked |
| `18-pbe` / `pbe` | `PBE` | set en cours de test sur le PBE |
| `17`, `16` | `1100` + `set=TFTSet17` | set passe, tant que l'API le sert |

`sets.json` ne fait que declarer ces raccourcis (libelle + overrides `queue` /
`patch` / `rank`). Les unites, traits et items sont toujours lus depuis l'API, ce
qui evite de coder en dur des noms qui changent : le Set 17 utilise `TFT17_Jinx`
et `TFT_Item_X`, le Set 18 PBE melange `DA_18_Ahri`, `DA_Karma18`, `DA_LichBane`
et `DA_18_EmblemCoven`. Les comps sont resolues par **nom normalise**, donc un
`champ: "Ahri"` retrouve `DA_18_Ahri` sans configuration.

Les comps de `comps.json` restent liees a un set : `--validate` liste ce qui a
disparu, `--discover N` recree une base de depart, a affiner ensuite dans
l'explorer MetaTFT (coller la nouvelle URL dans `explorer_url`). Une comp qui
cite une unite ou un trait absent du set cible est **refusee** plutot
qu'analysee avec des filtres morts (`--force` pour outrepasser).
