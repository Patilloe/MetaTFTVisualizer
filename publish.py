#!/usr/bin/env python3
"""
Publication des rapports TFT : site statique GitHub Pages + annonces Discord.

Le module est volontairement independant de `metatft.py` : il ne lit que le
`summary.csv` produit par une generation, ce qui permet de le lancer seul sur
un dossier deja genere.

    python publish.py --site output_set18_pbe --dry-run          # simulation
    python publish.py --site output_set18_pbe --deploy --notify  # pour de vrai

Secrets attendus dans l'environnement (jamais dans le depot) :
    DISCORD_WEBHOOK_GENERAL   webhook du channel general
    DISCORD_WEBHOOK_<SLUG>    webhook d'un channel de comp (cf. comp_channels.json)
    SITE_URL                  ex: https://user.github.io/metatft
    GITHUB_TOKEN              seulement si le remote est en HTTPS avec token
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Iterable
from urllib.parse import quote

import requests

SNAPSHOT_DEFAULT = os.path.join("data", "last_run.json")
CHANNELS_DEFAULT = "comp_channels.json"

# Un ecart de place moyenne en dessous de ce seuil n'est pas une information :
# c'est du bruit d'echantillonnage d'un jour a l'autre.
DEFAULT_THRESHOLD = 0.10

# meme paire divergente que les graphiques et les pages : bleu / rouge, le
# vert/rouge etant indistinguable en vision daltonienne
COLOR_BETTER = 0x3987E5
COLOR_WORSE = 0xE66767
COLOR_NEUTRAL = 0x898781


# --------------------------------------------------------------------------- #
# Lecture d'une generation
# --------------------------------------------------------------------------- #

def read_summary(site_dir: str) -> dict[str, dict[str, Any]]:
    """summary.csv -> {nom de comp: metriques}. Les comps en erreur sont ignorees."""
    path = os.path.join(site_dir, "summary.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} introuvable — lance d'abord metatft.py")

    out: dict[str, dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("erreur"):
                continue
            try:
                out[row["comp"]] = {
                    "champ": row.get("champ", ""),
                    "games": int(row.get("games") or 0),
                    "avg_place": float(row["avg_place"]),
                    "top4": float(row.get("top4") or 0.0),
                    "win": float(row.get("win") or 0.0),
                }
            except (KeyError, ValueError):
                continue
    return out


# --------------------------------------------------------------------------- #
# Comparaison avec la generation precedente
# --------------------------------------------------------------------------- #

@dataclass
class Change:
    comp: str
    champ: str
    avg_place: float
    previous: float | None      # None = nouvelle entree
    delta: float                # negatif = la comp s'est amelioree
    games: int
    top4: float

    @property
    def is_new(self) -> bool:
        return self.previous is None

    def line(self) -> str:
        if self.is_new:
            return f"**{self.comp}** — nouvelle entree, place {self.avg_place:.2f}"
        # forme et non couleur : lisible aussi en vision daltonienne
        arrow = "▼" if self.delta < 0 else "▲"
        return (f"{arrow} **{self.comp}** {self.previous:.2f} → "
                f"{self.avg_place:.2f} ({self.delta:+.2f})")


def load_snapshot(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    # utf-8-sig : ces fichiers sont souvent edites a la main sous Windows,
    # qui y laisse un BOM que json refuse
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_snapshot(path: str, comps: dict[str, dict], meta: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "meta": meta, "comps": comps}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def diff_runs(previous: dict[str, Any], current: dict[str, dict],
              threshold: float = DEFAULT_THRESHOLD) -> list[Change]:
    """Changements notables entre deux generations, tries par ampleur.

    Une nouvelle comp compte comme un changement ; une comp disparue est
    ignoree (elle sort souvent juste sous le seuil d'echantillon).
    """
    old = (previous or {}).get("comps", {})
    changes: list[Change] = []
    for comp, cur in current.items():
        before = old.get(comp)
        if before is None:
            changes.append(Change(comp, cur["champ"], cur["avg_place"], None,
                                  0.0, cur["games"], cur["top4"]))
            continue
        delta = cur["avg_place"] - float(before["avg_place"])
        if abs(delta) >= threshold:
            changes.append(Change(comp, cur["champ"], cur["avg_place"],
                                  float(before["avg_place"]), delta,
                                  cur["games"], cur["top4"]))
    changes.sort(key=lambda c: (c.is_new, -abs(c.delta)))
    return changes


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

def report_url(site_url: str, comp: str) -> str:
    return f"{site_url.rstrip('/')}/{quote(comp)}/report.html"


def build_general_embed(site_url: str, changes: list[Change], set_label: str,
                        comp_count: int, max_movers: int = 8) -> dict:
    movers = changes[:max_movers]
    if movers:
        description = "\n".join(c.line() for c in movers)
        if len(changes) > len(movers):
            description += f"\n… et {len(changes) - len(movers)} autre(s)"
    else:
        description = "Aucun mouvement notable depuis la derniere generation."

    improved = sum(1 for c in changes if not c.is_new and c.delta < 0)
    worsened = sum(1 for c in changes if not c.is_new and c.delta > 0)
    color = COLOR_NEUTRAL
    if improved or worsened:
        color = COLOR_BETTER if improved >= worsened else COLOR_WORSE

    return {
        "title": f"Recap TFT — {set_label}",
        "url": site_url,
        "description": description,
        "color": color,
        "fields": [
            {"name": "Comps analysees", "value": str(comp_count), "inline": True},
            {"name": "Mouvements", "value": str(len(changes)), "inline": True},
        ],
        "footer": {"text": "place moyenne, negatif = amelioration"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_comp_embed(change: Change, site_url: str) -> dict:
    color = COLOR_NEUTRAL if change.is_new else (
        COLOR_BETTER if change.delta < 0 else COLOR_WORSE)
    fields = [
        {"name": "Place moyenne", "value": f"{change.avg_place:.2f}", "inline": True},
        {"name": "Top 4", "value": f"{100 * change.top4:.1f} %", "inline": True},
        {"name": "Parties", "value": f"{change.games}", "inline": True},
    ]
    if not change.is_new:
        fields.insert(1, {"name": "Variation",
                          "value": f"{change.delta:+.2f} (avant {change.previous:.2f})",
                          "inline": True})
    return {
        "title": change.comp,
        "url": report_url(site_url, change.comp),
        "color": color,
        "fields": fields,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def post_webhook(webhook_url: str, embed: dict, dry_run: bool = False,
                 label: str = "") -> bool:
    """Poste un embed. En dry-run, affiche ce qui serait envoye."""
    if dry_run or not webhook_url:
        reason = "dry-run" if dry_run else "webhook absent"
        print(f"  [{reason}] {label or embed.get('title', '')}")
        print("    " + json.dumps(embed, ensure_ascii=False)[:400])
        return False
    r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=20)
    if r.status_code >= 300:
        print(f"  ✗ webhook {label}: HTTP {r.status_code} {r.text[:200]}",
              file=sys.stderr)
        return False
    print(f"  ✓ poste : {label or embed.get('title', '')}")
    return True


def post_general_update(webhook_url: str, site_url: str, top_movers: list[Change],
                        set_label: str = "", comp_count: int = 0,
                        dry_run: bool = False) -> bool:
    """Annonce de la generation dans le channel general."""
    embed = build_general_embed(site_url, top_movers, set_label, comp_count)
    return post_webhook(webhook_url, embed, dry_run=dry_run, label="general")


# --------------------------------------------------------------------------- #
# Routage par channel de comp
# --------------------------------------------------------------------------- #

def load_channels(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def channel_webhook(slug: str, cfg: dict) -> str:
    """Le webhook vient de l'environnement ; le JSON ne porte que le nom de
    la variable, jamais l'URL."""
    env_key = cfg.get("webhook_env") or f"DISCORD_WEBHOOK_{slug.upper().replace('-', '_')}"
    return os.environ.get(env_key, "")


def route_changes(changes: list[Change], channels: dict[str, dict]) -> dict[str, list[Change]]:
    """Associe chaque changement au(x) channel(s) de comp concerne(s)."""
    routed: dict[str, list[Change]] = {}
    for slug, cfg in channels.items():
        members = {m.lower() for m in cfg.get("champions", [])}
        for change in changes:
            if change.champ.lower() in members or change.comp.lower() in members:
                routed.setdefault(slug, []).append(change)
    return routed


# --------------------------------------------------------------------------- #
# Deploiement GitHub Pages
# --------------------------------------------------------------------------- #

def run_git(args: list[str], cwd: str, dry_run: bool,
            report_error: bool = True) -> tuple[int, str]:
    """`report_error=False` pour les commandes dont un code non nul est une
    reponse et non un echec (ex: `diff --quiet`)."""
    if dry_run:
        print(f"  [dry-run] git {' '.join(args)}")
        return 0, ""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0 and report_error:
        print(f"  ✗ git {' '.join(args)}: {proc.stderr.strip()}", file=sys.stderr)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def deploy(site_dir: str, docs_dir: str, repo_root: str, message: str,
           dry_run: bool = False, push: bool = True) -> bool:
    """Copie la generation dans `docs/` puis commit (et push).

    GitHub Pages est configure sur `main` + dossier `/docs` : pas de branche
    orpheline a gerer, le site suit le meme historique que le code.
    """
    if not os.path.isdir(site_dir):
        print(f"  ✗ {site_dir} introuvable", file=sys.stderr)
        return False

    if not os.path.isdir(os.path.join(repo_root, ".git")):
        msg = ("pas un depot git — `git init` puis ajout d'un remote requis "
               "avant un vrai --deploy")
        if not dry_run:
            print(f"  ✗ {msg}", file=sys.stderr)
            return False
        print(f"  [dry-run] {msg}")

    if not dry_run:
        if os.path.isdir(docs_dir):
            shutil.rmtree(docs_dir)
        shutil.copytree(site_dir, docs_dir)
        # empeche Jekyll de masquer les dossiers commencant par un underscore
        open(os.path.join(docs_dir, ".nojekyll"), "w").close()
    print(f"  {'[dry-run] ' if dry_run else ''}copie {site_dir} -> {docs_dir}")

    rel = os.path.relpath(docs_dir, repo_root)
    run_git(["add", rel], repo_root, dry_run)
    code, _ = run_git(["diff", "--cached", "--quiet"], repo_root, dry_run,
                      report_error=False)
    if code == 0 and not dry_run:
        print("  rien de nouveau a committer")
        return True

    if run_git(["commit", "-m", message], repo_root, dry_run)[0] != 0:
        return False
    if push and run_git(["push"], repo_root, dry_run)[0] != 0:
        return False
    return True


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def publish(site_dir: str, *, site_url: str = "", set_label: str = "",
            snapshot_path: str = SNAPSHOT_DEFAULT,
            channels_path: str = CHANNELS_DEFAULT,
            threshold: float = DEFAULT_THRESHOLD,
            notify: bool = False, do_deploy: bool = False,
            docs_dir: str = "docs", repo_root: str = ".",
            dry_run: bool = False, force: bool = False,
            push: bool = True) -> int:
    """Diff, deploiement, notifications. Retourne le nombre de changements."""
    current = read_summary(site_dir)
    previous = load_snapshot(snapshot_path)
    bootstrap = not previous
    changes = diff_runs(previous, current, threshold)

    print(f"{len(current)} comps dans {site_dir}, "
          f"{len(changes)} changement(s) au-dela de {threshold:.2f} place")
    for change in changes[:10]:
        print("   " + change.line().replace("**", ""))

    if not changes and not force:
        print("Rien de significatif : ni deploiement ni notification.")
        return 0

    if bootstrap and not force:
        # sans snapshot precedent, toutes les comps sont "nouvelles" : on
        # publie le site mais on n'inonde pas Discord de 20 annonces.
        print("Premiere execution : snapshot de reference, pas de notification "
              "(--force pour annoncer quand meme).")
        notify = False

    if do_deploy:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        deploy(site_dir, docs_dir, repo_root,
               f"recap TFT {stamp} ({len(changes)} mouvement(s))",
               dry_run=dry_run, push=push)

    if notify:
        site_url = site_url or os.environ.get("SITE_URL", "")
        if not site_url:
            print("  ⚠ SITE_URL absent : les liens des embeds seront vides",
                  file=sys.stderr)
        post_general_update(os.environ.get("DISCORD_WEBHOOK_GENERAL", ""),
                            site_url, changes, set_label, len(current),
                            dry_run=dry_run)

        channels = load_channels(channels_path)
        for slug, slug_changes in route_changes(changes, channels).items():
            hook = channel_webhook(slug, channels[slug])
            for change in slug_changes:
                post_webhook(hook, build_comp_embed(change, site_url),
                             dry_run=dry_run, label=f"{slug}/{change.comp}")

    if not dry_run:
        save_snapshot(snapshot_path, current, {"set_label": set_label,
                                               "site_dir": site_dir})
    return len(changes)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", required=True, help="dossier genere par metatft.py")
    p.add_argument("--site-url", default="", help="URL publique (sinon $SITE_URL)")
    p.add_argument("--set-label", default="")
    p.add_argument("--snapshot", default=SNAPSHOT_DEFAULT)
    p.add_argument("--channels", default=CHANNELS_DEFAULT)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--deploy", action="store_true", help="copie dans docs/ + commit")
    p.add_argument("--no-push", dest="push", action="store_false")
    p.add_argument("--docs-dir", default="docs")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--notify", action="store_true", help="poste sur Discord")
    p.add_argument("--force", action="store_true",
                   help="publie meme sans changement significatif")
    p.add_argument("--dry-run", action="store_true",
                   help="n'ecrit rien, ne poste rien, ne pousse rien")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    cfg = parse_args(argv)
    publish(cfg.site, site_url=cfg.site_url, set_label=cfg.set_label,
            snapshot_path=cfg.snapshot, channels_path=cfg.channels,
            threshold=cfg.threshold, notify=cfg.notify, do_deploy=cfg.deploy,
            docs_dir=cfg.docs_dir, repo_root=cfg.repo_root,
            dry_run=cfg.dry_run, force=cfg.force, push=cfg.push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
