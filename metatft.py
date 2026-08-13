#!/usr/bin/env python3
"""
Analyse fine des compositions TFT a partir de l'API MetaTFT Explorer.

Nouveautes par rapport a la version initiale :
  - set non code en dur (auto-detection du set live, ou --set 18)
  - traduction complete des filtres de l'URL explorer (unit_item, extra_traits, ...)
  - statistiques avec intervalles de confiance, z-scores, correction FDR et
    lissage bayesien (les builds a faible echantillon ne remontent plus par hasard)
  - analyses supplementaires : lift marginal par item, paires d'items, niveau
    d'etoile du carry, traits flex, unites flex
  - cache disque (JSON + icones), retries, parallelisation
  - exports CSV / JSON / PNG / HTML

Usage :
    python metatft.py                       # analyse comps.json sur le set live
    python metatft.py --set 18              # force le Set 18
    python metatft.py --validate            # verifie quelles comps existent encore
    python metatft.py --discover 30         # genere un comps.json pour le set courant
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse, parse_qs, urlencode, quote

import requests

API_BASE = "https://api-hc.metatft.com/tft-explorer-api/"
CDN_ITEM = "https://cdn.metatft.com/file/metatft/items/{name}.png"

TAB_BUILDS = "unit_builds"
TAB_ITEMS_UNIQUE = "unit_items_unique"

SET_PREFIX_RE = re.compile(r"\bTFT\d+_")

# Composants de base : stables d'un set a l'autre, compares sur le nom normalise
# (TFT_Item_BFSword, DA_BFSword...).
COMPONENT_NAMES = {
    "bfsword", "recurvebow", "needlesslylargerod", "tearofthegoddess",
    "chainvest", "negatroncloak", "giantsbelt", "sparringgloves", "spatula",
    "fryingpan",
}


# --------------------------------------------------------------------------- #
# Statistiques
# --------------------------------------------------------------------------- #

def _norm_sf(z: float) -> float:
    """P(Z > z) pour une loi normale centree reduite."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson pour une proportion (robuste a n petit)."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


@dataclass
class Stats:
    """Statistiques derivees d'un vecteur placement_count (8 positions)."""
    n: int
    counts: list[int]
    avg: float
    sd: float
    sem: float
    win: float
    top2: float
    top4: float
    top4_lo: float
    top4_hi: float

    @classmethod
    def from_counts(cls, counts: Sequence[int]) -> "Stats":
        counts = [int(c or 0) for c in counts]
        n = sum(counts)
        if n == 0:
            return cls(0, counts, float("nan"), 0.0, float("inf"), 0.0, 0.0, 0.0, 0.0, 1.0)
        avg = sum((i + 1) * c for i, c in enumerate(counts)) / n
        var = sum(c * ((i + 1) - avg) ** 2 for i, c in enumerate(counts)) / n
        sd = math.sqrt(var)
        sem = sd / math.sqrt(n) if n else float("inf")
        top4_k = sum(counts[:4])
        lo, hi = wilson(top4_k, n)
        return cls(
            n=n, counts=counts, avg=avg, sd=sd, sem=sem,
            win=counts[0] / n, top2=sum(counts[:2]) / n, top4=top4_k / n,
            top4_lo=lo, top4_hi=hi,
        )

    def compare(self, base: "Stats", prior: float = 30.0) -> dict[str, float]:
        """Compare a une baseline : delta, significativite, valeur lissee.

        `prior` est le poids (en parties) de la baseline dans le lissage
        empirique-bayesien. Il est calibre sur la taille de la comp (voir
        `effective_prior`) et non fixe : a 50 000 parties de comp, un build
        vu 500 fois doit etre rappele fortement vers la moyenne, un build vu
        25 000 fois presque pas.
        """
        if self.n == 0 or base.n == 0:
            return {"d_avg": 0.0, "z": 0.0, "p": 1.0, "d_top4": 0.0,
                    "d_win": 0.0, "shrunk_avg": base.avg, "shrunk_d_avg": 0.0,
                    "ci_lo": 0.0, "ci_hi": 0.0}
        d_avg = self.avg - base.avg
        se = math.sqrt(self.sem ** 2 + base.sem ** 2) or 1e-9
        z = d_avg / se
        p = 2 * _norm_sf(abs(z))
        shrunk = (self.n * self.avg + prior * base.avg) / (self.n + prior)
        return {
            "d_avg": d_avg,
            "z": z,
            "p": p,
            "d_top4": self.top4 - base.top4,
            "d_win": self.win - base.win,
            "shrunk_avg": shrunk,
            "shrunk_d_avg": shrunk - base.avg,
            "ci_lo": d_avg - 1.96 * se,
            "ci_hi": d_avg + 1.96 * se,
        }


def effective_prior(base_n: int, prior_min: float, prior_frac: float) -> float:
    """Force du lissage, exprimee en parties, proportionnelle a la comp.

    Un prior fixe ne veut rien dire : 30 parties pesent enormement face a une
    ligne vue 40 fois et rien du tout face a une ligne vue 40 000 fois. On
    prend donc une fraction de l'echantillon de la comp, avec un plancher.
    Avec `prior_frac = 0.02` sur une comp a 50 000 parties, le prior vaut
    1 000 parties : un build a 1 % de part (500 parties) garde un tiers de sa
    difference, un build a 50 % la garde presque entierement.
    """
    return max(prior_min, prior_frac * base_n)


def fdr_bh(pvals: Sequence[float], alpha: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg : quelles lignes restent significatives apres
    correction pour tests multiples (on teste des dizaines de builds)."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    keep = [False] * m
    threshold = 0
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= alpha * rank / m:
            threshold = rank
    for rank, idx in enumerate(order, start=1):
        keep[idx] = rank <= threshold
    return keep


# --------------------------------------------------------------------------- #
# Client API
# --------------------------------------------------------------------------- #

class ApiClient:
    def __init__(self, cache_dir: str, ttl: float = 3600.0, timeout: float = 25.0,
                 retries: int = 3, verbose: bool = False):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "metatft-analysis/2.0"})
        self._no_icon: set[str] = set()
        os.makedirs(os.path.join(cache_dir, "json"), exist_ok=True)
        os.makedirs(os.path.join(cache_dir, "img"), exist_ok=True)

    def _cache_path(self, key: str, kind: str, ext: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, kind, f"{digest}.{ext}")

    def get_json(self, path: str, params: Sequence[tuple[str, str]]) -> dict[str, Any]:
        query = urlencode(params, safe=".*!,|-")
        url = f"{API_BASE}{path}?{query}"
        cache = self._cache_path(url, "json", "json")
        if self.ttl > 0 and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < self.ttl:
            with open(cache, "r", encoding="utf-8") as f:
                return json.load(f)

        last = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(url, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return data
            except Exception as exc:  # noqa: BLE001
                last = exc
                if self.verbose:
                    print(f"    retry {attempt + 1}/{self.retries} sur {path}: {exc}")
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"echec API {url}: {last}")

    def get_item_image(self, item_name: str):
        from PIL import Image  # import tardif : pas requis en mode --no-plots

        if item_name in self._no_icon:
            raise FileNotFoundError(item_name)
        cache = self._cache_path(item_name, "img", "png")
        if not os.path.exists(cache):
            try:
                self._fetch_icon(item_name, cache)
            except Exception:
                self._no_icon.add(item_name)   # evite de re-taper le CDN en 404
                raise
        return Image.open(cache).convert("RGBA")

    def _fetch_icon(self, item_name: str, cache: str) -> None:
        r = self.session.get(CDN_ITEM.format(name=item_name.lower()), timeout=self.timeout)
        r.raise_for_status()
        with open(cache, "wb") as f:
            f.write(r.content)


# --------------------------------------------------------------------------- #
# Set / filtres
# --------------------------------------------------------------------------- #

def norm_name(raw: str) -> str:
    """Reduit un identifiant API a son nom "nu", quel que soit le set.

    Les schemas de nommage changent d'un set a l'autre, et meme a l'interieur
    d'un set sur le PBE :
        TFT17_Jinx, DA_18_Ahri, DA_Karma18, DA_18_Akali_AD, TFT18_Gromp
        TFT_Item_LichBane, DA_LichBane, DA_18_EmblemCoven
    On compare donc sur le nom normalise plutot que sur un prefixe.
    """
    s = raw
    s = re.sub(r"^(?:TFT\d*|DA)_", "", s)          # TFT17_ / TFT_ / DA_
    s = re.sub(r"^\d+_", "", s)                     # DA_18_ -> 18_
    s = re.sub(r"^(?:Item|Artifact|Augment)_", "", s)
    s = re.sub(r"_(?:AD|AP)$", "", s)               # Akali_AD
    s = re.sub(r"\d+", "", s)                       # Karma18
    return re.sub(r"[^a-z]", "", s.lower())


@dataclass
class SetProfile:
    key: str                          # valeur passee a --set, ou "live"
    label: str
    set_param: str | None = None
    params: list[tuple[str, str]] = field(default_factory=list)
    units: set[str] = field(default_factory=set)
    traits: set[str] = field(default_factory=set)
    items: set[str] = field(default_factory=set)
    _unit_idx: dict[str, str] = field(default_factory=dict)
    _trait_idx: dict[str, str] = field(default_factory=dict)
    _item_idx: dict[str, str] = field(default_factory=dict)

    def index(self) -> None:
        for name in self.units:
            self._unit_idx.setdefault(norm_name(name), name)
        for name in self.traits:
            self._trait_idx.setdefault(norm_name(name), name)
        for name in self.items:
            self._item_idx.setdefault(norm_name(name), name)

    def resolve_unit(self, token: str) -> str | None:
        return token if token in self.units else self._unit_idx.get(norm_name(token))

    def resolve_trait(self, token: str) -> str | None:
        return token if token in self.traits else self._trait_idx.get(norm_name(token))

    def resolve_item(self, token: str) -> str | None:
        return token if token in self.items else self._item_idx.get(norm_name(token))

    @property
    def unit_prefix(self) -> str:
        """Prefixe majoritaire, utilise seulement pour l'affichage."""
        counter: dict[str, int] = {}
        for name in self.units:
            m = re.match(r"^(?:TFT\d*|DA)_(?:\d+_)?", name)
            if m:
                counter[m.group(0)] = counter.get(m.group(0), 0) + 1
        return max(counter, key=counter.get, default="")


def load_set_profiles(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def resolve_set(api: ApiClient, base_params: list[tuple[str, str]],
                profiles: dict[str, dict], forced: str | None) -> SetProfile:
    """Selectionne le set et charge ses unites / traits / items.

    Un set ne se designe pas par un prefixe mais par un couple (queue, patch) :
    le Set 18 en test vit sur `queue=PBE`, la saison live sur `queue=1100`.
    """
    meta = profiles.get(forced or "", {}) if forced else {}
    if forced and not meta:
        digits = re.sub(r"\D", "", forced)
        meta = {"label": f"Set {digits}", "set_param": f"TFTSet{digits}"} if digits else {}

    overrides = [(k, str(v)) for k, v in meta.items()
                 if k in ("queue", "patch", "days", "rank")]
    params = [(k, v) for k, v in base_params
              if k not in {k2 for k2, _ in overrides}] + overrides
    if meta.get("set_param"):
        params.append(("set", meta["set_param"]))

    profile = SetProfile(
        key=forced or "live",
        label=meta.get("label", "set live"),
        set_param=meta.get("set_param"),
        params=params,
    )
    profile.units = {r["units"] for r in api.get_json("units", params).get("data", [])
                     if r.get("units")}
    profile.traits = {re.sub(r"_\d+$", "", r["traits"])
                      for r in api.get_json("traits", params).get("data", [])
                      if r.get("traits")}
    profile.items = {r["items"] for r in api.get_json("items", params).get("data", [])
                     if r.get("items")}
    profile.index()
    if not forced and profile.units:
        profile.label = f"set live ({profile.unit_prefix.rstrip('_')})"
    return profile


# les jokers `.*` de l'URL explorer font partie des groupes etoiles / nb items
UNIT_FILTER_RE = re.compile(r"^(?P<unit>.+?)_(?P<stars>[0-9x,.*]+)_(?P<items>[0-9x,.*]+)$")
TRAIT_TIER_RE = re.compile(r"^(?P<trait>.+)_(?P<tier>\d+)$")
EXTRA_TRAIT_RE = re.compile(r"^(?P<trait>.+?)-(?P<rest>\d+plus)$", re.I)


def _retarget(token: str, kind: str, profile: SetProfile,
              missing: list[str]) -> str | None:
    """Reecrit un identifiant vers le set cible (TFT16_Viego -> DA_18_...).

    Retourne None et alimente `missing` si l'element n'existe pas dans le set.
    """
    base = re.sub(r"-\d+$", "", token)
    suffix = token[len(base):]
    resolver = {"unit": profile.resolve_unit, "trait": profile.resolve_trait,
                "item": profile.resolve_item}[kind]
    found = resolver(base)
    if found is None:
        missing.append(f"{kind}:{base}")
        return None
    return found + suffix


def translate_filter(key: str, value: str, profile: SetProfile,
                     missing: list[str]) -> str | None:
    """Traduit une valeur de filtre explorer vers le set cible."""
    neg = ""
    if value.startswith("!"):
        neg, value = "!", value[1:]

    if key == "unit":
        m = UNIT_FILTER_RE.match(value)
        unit_tok, stars, items = (m["unit"], m["stars"], m["items"]) if m else (value, "x", "x")
        unit_tok = _retarget(unit_tok, "unit", profile, missing)
        if unit_tok is None:
            return None
        # `<unite>-1_<etoiles>_<nb items>`, une entree par combinaison, `x` = joker
        out = [f"{unit_tok}_{'.*' if s in ('x', '') else s}_{'.*' if n in ('x', '') else n}"
               for s in stars.split(",") for n in items.split(",")]
        return neg + ",".join(out)

    if key == "trait":
        parts = []
        for tok in value.split(","):
            m = TRAIT_TIER_RE.match(tok)
            trait, tier = (m["trait"], "_" + m["tier"]) if m else (tok, "")
            trait = _retarget(trait, "trait", profile, missing)
            if trait is None:
                return None
            parts.append(trait + tier)
        return neg + ",".join(parts)

    if key == "extra_traits":
        m = EXTRA_TRAIT_RE.match(value)
        if not m:
            return neg + value
        trait = _retarget(m["trait"], "trait", profile, missing)
        return None if trait is None else f"{neg}{trait}-{m['rest']}"

    if key == "unit_item":
        left, sep, right = value.partition("&")
        left = _retarget(left, "unit", profile, missing)
        if not sep:
            return None if left is None else neg + left
        right = _retarget(right, "item", profile, missing)
        if left is None or right is None:
            return None
        return f"{neg}{left}&{right}"

    if key == "item":
        item = _retarget(value, "item", profile, missing)
        return None if item is None else neg + item

    if key == "item_holder_unit":
        unit = _retarget(value, "unit", profile, missing)
        return None if unit is None else neg + unit

    return neg + value


# Correspondance parametre URL explorer -> parametre API.
EXPLORER_TO_API = {
    "unit": "unit_tier_numitems_unique",
    "trait": "trait",
    "unit_item": "unit_item_unique",
    "extra_traits": "extra_traits",
    "item": "item",
    "augment": "augment",
    "level": "level",
    "item_holder": "item_holder",
    "item_holder_unit": "item_holder_unit",
    "stage": "stage",
    "num_unit_slots": "num_unit_slots",
    "duplicate_unit_count": "duplicate_unit_count",
    "extra_traits_count": "extra_traits_count",
    "3_star_count": "3_star_count",
    "4_star_count": "4_star_count",
}
EXPLORER_IGNORED = {"tab", "sortby", "sort", "page", "compact"}


def comp_filters(comp: dict, profile: SetProfile) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Traduit l'URL explorer d'une comp en parametres API.

    Retourne (parametres, elements introuvables dans le set, parametres inconnus).
    La version initiale ne traduisait que `unit` et `trait` : les exclusions
    `unit_item=!...` etaient perdues en silence, ce qui faussait la baseline.
    """
    params = parse_qs(urlparse(comp.get("explorer_url", "")).query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    missing: list[str] = []
    unknown: list[str] = []
    for key, values in params.items():
        if key in EXPLORER_IGNORED:
            continue
        api_key = EXPLORER_TO_API.get(key)
        if api_key is None:
            unknown.append(key)
            continue
        for value in values:
            translated = translate_filter(key, value, profile, missing)
            if translated is not None:
                out.append((api_key, translated))
    return out, sorted(set(missing)), sorted(set(unknown))


def comp_problems(comp: dict, profile: SetProfile) -> list[str]:
    """Liste ce qui, dans une comp, n'existe pas dans le set cible.

    Sans ce garde-fou, une comp d'un ancien set dont le carry existe encore
    (Illaoi, Zoe, Graves...) s'analyse quand meme : les filtres disparus ne
    matchent plus rien et sont ignores en silence, on obtient des chiffres du
    nouveau set sous une definition de comp de l'ancien.
    """
    problems = []
    if profile.units and profile.resolve_unit(comp["champ"]) is None:
        problems.append(f"carry absent ({comp['champ']})")
    _, missing, _ = comp_filters(comp, profile)
    if missing:
        problems.append("filtres introuvables dans le set: " + ", ".join(missing))
    return problems


# --------------------------------------------------------------------------- #
# Parsing des items
# --------------------------------------------------------------------------- #

def strip_copy_suffix(item: str) -> str:
    return re.sub(r"-\d+$", "", item)


def parse_build(raw: str | None) -> list[str]:
    """`TFT17_Jinx&A|B|C` -> ['A', 'B', 'C']."""
    if not raw:
        return []
    _, _, items = raw.partition("&")
    return [i for i in items.split("|") if i]


def parse_unique_item(raw: str | None) -> str | None:
    """`TFT17_Jinx-1&TFT_Item_X-1` -> `TFT_Item_X-1`."""
    if not raw:
        return None
    _, sep, item = raw.partition("&")
    return item if sep else None


def item_category(item: str, profile: SetProfile) -> str:
    """radiant / artifact / emblem / mechanic / component / craftable / duplicate."""
    if re.search(r"-[23]$", item):
        return "duplicate"
    base = strip_copy_suffix(item)
    if "Emblem" in base:
        return "emblem"
    if "Radiant" in base:
        return "radiant"
    if "Artifact" in base or "Darkin" in base or "Ornn" in base:
        return "artifact"
    if norm_name(base) in COMPONENT_NAMES:
        return "component"
    # items propres a la mecanique du set (AnimaSquadItem, BlastPotion18...)
    if re.match(r"^(?:TFT\d+|DA)_(?:\d+_)?", base) and re.search(r"Item|Potion|Tier\d", base):
        return "mechanic"
    return "craftable"


def pretty_item(item: str) -> str:
    base = strip_copy_suffix(item)
    base = re.sub(r"^(?:TFT\d*|DA)_(?:\d+_)?", "", base)
    base = re.sub(r"^(?:Item|Artifact)_", "", base)
    base = re.sub(r"\d+(?=$|_)", "", base)   # Amumu18, Vi18 -> Amumu, Vi
    base = base.replace("_", " ")
    return re.sub(r"(?<!^)(?=[A-Z])", " ", base).replace("  ", " ").strip()


def pretty_label(items: Sequence[str]) -> str:
    return " + ".join(pretty_item(i) for i in items) if items else "(aucun item)"


# --------------------------------------------------------------------------- #
# Construction des tableaux d'analyse
# --------------------------------------------------------------------------- #

def build_rows(entries: Iterable[tuple[tuple[str, ...], list[int]]], base: Stats,
               prior_min: float, prior_frac: float, min_games: int,
               alpha: float) -> list[dict]:
    """Transforme {cle -> placement_count} en lignes enrichies et triees."""
    prior = effective_prior(base.n, prior_min, prior_frac)
    rows = []
    for key, counts in entries:
        st = Stats.from_counts(counts)
        if st.n < min_games:
            continue
        cmp_ = st.compare(base, prior=prior)
        share = st.n / base.n if base.n else 0.0
        rows.append({
            "key": list(key),
            "label": pretty_label(key),
            "n": st.n,
            "share": share,
            "avg_place": round(st.avg, 3),
            "sd": round(st.sd, 3),
            "win": round(st.win, 4),
            "top2": round(st.top2, 4),
            "top4": round(st.top4, 4),
            "top4_ci": [round(st.top4_lo, 4), round(st.top4_hi, 4)],
            "d_avg": round(cmp_["d_avg"], 3),
            "d_avg_ci": [round(cmp_["ci_lo"], 3), round(cmp_["ci_hi"], 3)],
            "d_top4": round(cmp_["d_top4"], 4),
            "d_win": round(cmp_["d_win"], 4),
            "shrunk_avg": round(cmp_["shrunk_avg"], 3),
            "score": round(cmp_["shrunk_d_avg"], 3),
            # estimation pessimiste : la borne la moins flatteuse de l'IC 95%.
            # Un petit echantillon doit "prouver" son avantage pour remonter.
            "d_avg_pess": round(min(0.0, cmp_["ci_hi"]) if cmp_["d_avg"] < 0
                                else max(0.0, cmp_["ci_lo"]), 3),
            # gain de places apporte a la comp entiere par cette ligne
            "impact": round(-cmp_["shrunk_d_avg"] * share, 4),
            "z": round(cmp_["z"], 2),
            "p": cmp_["p"],
        })
    keep = fdr_bh([r["p"] for r in rows], alpha=alpha)
    for row, sig in zip(rows, keep):
        row["significant"] = bool(sig)
        row["p"] = round(row["p"], 5)
    rows.sort(key=lambda r: r["score"])
    return rows


def aggregate(entries: Iterable[tuple[tuple[str, ...], Sequence[int]]]) -> dict[tuple, list[int]]:
    acc: dict[tuple, list[int]] = {}
    for key, counts in entries:
        slot = acc.setdefault(key, [0] * 8)
        for i, c in enumerate(counts[:8]):
            slot[i] += int(c or 0)
    return acc


def top_selection(rows: list[dict], n_common: int, n_hidden: int) -> list[dict]:
    """Les N builds les plus joues + N pepites (bon score, hors du top joue)."""
    by_play = sorted(rows, key=lambda r: r["n"], reverse=True)[:n_common]
    seen = {tuple(r["key"]) for r in by_play}
    hidden = [r for r in rows if tuple(r["key"]) not in seen and r["score"] < 0][:n_hidden]
    for r in by_play:
        r["group"] = "populaire"
    for r in hidden:
        r["group"] = "pepite"
    return by_play + hidden


# --------------------------------------------------------------------------- #
# Analyse d'une comp
# --------------------------------------------------------------------------- #

def analyse_comp(comp: dict, api: ApiClient, profile: SetProfile,
                 base_params: list[tuple[str, str]], cfg: argparse.Namespace) -> dict:
    name = comp["name"]
    champ = comp["champ"]
    filters, missing, unknown = comp_filters(comp, profile)
    params = base_params + filters
    unit_id = profile.resolve_unit(champ) or champ

    result: dict[str, Any] = {
        "name": name, "champ": champ, "unit": unit_id,
        "set": profile.key, "set_label": profile.label,
        "warnings": [],
        "tables": {},
    }
    if unknown:
        result["warnings"].append(
            "parametres explorer non traduits: " + ", ".join(unknown))

    problems = comp_problems(comp, profile)
    if problems and not cfg.force:
        result["error"] = f"comp incompatible avec {profile.label} — " + " | ".join(problems)
        return result
    result["warnings"].extend(problems)

    # 1. baselines --------------------------------------------------------- #
    meta_total = Stats.from_counts(api.get_json("total", base_params)["data"][0]["placement_count"])
    base = Stats.from_counts(api.get_json("total", params)["data"][0]["placement_count"])
    result["meta"] = {
        "games": base.n,
        "share_of_meta": base.n / meta_total.n if meta_total.n else 0.0,
        "avg_place": round(base.avg, 3),
        "win": round(base.win, 4),
        "top4": round(base.top4, 4),
        "top4_ci": [round(base.top4_lo, 4), round(base.top4_hi, 4)],
        "d_avg_vs_meta": round(base.avg - meta_total.avg, 3),
        "meta_avg_place": round(meta_total.avg, 3),
    }
    if base.n < cfg.min_games:
        result["error"] = f"echantillon insuffisant ({base.n} parties)"
        return result

    def mk(entries):
        return build_rows(entries, base, cfg.prior, cfg.prior_frac,
                          cfg.min_games, cfg.alpha)

    result["meta"]["prior_games"] = round(
        effective_prior(base.n, cfg.prior, cfg.prior_frac))

    # 2. builds complets --------------------------------------------------- #
    builds_raw = api.get_json(f"{TAB_BUILDS}/{unit_id}", params).get("data", [])
    complete, partial = [], []
    for row in builds_raw:
        items = parse_build(row.get(TAB_BUILDS))
        target = complete if len(items) >= cfg.build_size else partial
        target.append((tuple(sorted(items)), row["placement_count"]))
    result["tables"]["builds"] = mk(aggregate(complete).items())
    result["tables"]["builds_partiels"] = mk(aggregate(partial).items())

    # 3. lift marginal par item et par paire (bien moins clairseme
    #    que les builds exacts : c'est la que se lit la vraie tendance)
    singles, pairs = [], []
    for items, counts in complete:
        for it in set(items):
            singles.append(((it,), counts))
        for pair in combinations(sorted(set(items)), 2):
            pairs.append((pair, counts))
    result["tables"]["item_lift"] = mk(aggregate(singles).items())
    result["tables"]["paires"] = mk(aggregate(pairs).items())

    # 4. items uniques par categorie --------------------------------------- #
    items_raw = api.get_json(f"{TAB_ITEMS_UNIQUE}/{unit_id}-1", params).get("data", [])
    by_cat: dict[str, list] = {}
    for row in items_raw:
        item = parse_unique_item(row.get(TAB_ITEMS_UNIQUE))
        if not item:
            continue
        cat = item_category(item, profile)
        if cat in ("duplicate", "component"):
            continue
        by_cat.setdefault(cat, []).append(((item,), row["placement_count"]))
    for cat, entries in by_cat.items():
        result["tables"][f"items_{cat}"] = mk(aggregate(entries).items())

    # 5. niveau d'etoile du carry ------------------------------------------ #
    tiers = []
    for row in api.get_json("unit_tier", params).get("data", []):
        tier = row.get("unit_tier")
        if tier and tier.startswith(unit_id + "_"):
            tiers.append(((f"{champ} {tier.rsplit('_', 1)[-1]}*",), row["placement_count"]))
    result["tables"]["etoiles"] = mk(aggregate(tiers).items())

    # 6. unites et traits flex --------------------------------------------- #
    def flex(tab: str, key: str) -> list[dict]:
        entries = []
        for row in api.get_json(tab, params).get("data", []):
            val = row.get(key)
            if not val:
                continue
            st_n = sum(row["placement_count"])
            # on ecarte ce qui est deja impose par le filtre de la comp
            if st_n >= 0.98 * base.n:
                continue
            entries.append(((val,), row["placement_count"]))
        return mk(aggregate(entries).items())

    if cfg.flex:
        result["tables"]["unites_flex"] = flex("units", "units")
        result["tables"]["traits_flex"] = flex("traits", "traits")

    return result


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #

CSV_FIELDS = ["group", "label", "n", "share", "avg_place", "shrunk_avg", "score",
              "d_avg", "d_avg_pess", "impact", "d_top4", "win", "top2", "top4",
              "sd", "z", "p", "significant"]


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def plot_ranked(rows: list[dict], title: str, path: str, api: ApiClient,
                icons: bool = True) -> None:
    """Barres horizontales du delta de place moyenne, avec IC 95% et icones."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import matplotlib.transforms as mtransforms
    from matplotlib.offsetbox import OffsetImage, AnnotationBbox

    if not rows:
        return
    rows = list(reversed(rows))  # meilleur en haut
    labels = [r["label"] for r in rows]
    deltas = [r["d_avg"] for r in rows]
    los = [r["d_avg_ci"][0] for r in rows]
    his = [r["d_avg_ci"][1] for r in rows]
    ys = list(range(len(rows)))

    span = max(0.35, max(abs(v) for v in deltas))
    norm = mcolors.TwoSlopeNorm(vmin=-span, vcenter=0.0, vmax=span)
    cmap = plt.get_cmap("RdYlGn_r")
    colors = [cmap(norm(d)) for d in deltas]

    # L'epaisseur de la barre porte la part d'echantillon : une ligne jouee
    # dans la moitie des parties doit dominer visuellement une ligne a 1 %,
    # meme si leurs places moyennes se ressemblent. Racine carree pour que les
    # petites lignes restent visibles malgre des ecarts de 1 a 1000.
    shares = [max(r["share"], 1e-6) for r in rows]
    ref = max(shares)
    heights = [0.16 + 0.72 * math.sqrt(s / ref) for s in shares]

    # trois colonnes alignees : barres | chiffres | icones. Une mise en page
    # en axes separes evite tout chevauchement, quelle que soit la longueur
    # des libelles d'items.
    fig, (ax, ax_txt, ax_img) = plt.subplots(
        1, 3, figsize=(15, max(4.0, 0.62 * len(rows) + 2.2)),
        gridspec_kw={"width_ratios": [1.0, 0.42, 0.10], "wspace": 0.02},
        sharey=True)
    for extra in (ax_txt, ax_img):
        extra.axis("off")

    ax.barh(ys, deltas, height=heights, color=colors, edgecolor="black",
            linewidth=0.6, zorder=3)
    ax.errorbar(deltas, ys, xerr=[[d - lo for d, lo in zip(deltas, los)],
                                  [hi - d for d, hi in zip(deltas, his)]],
                fmt="none", ecolor="black", elinewidth=1.0, capsize=3, zorder=4)
    ax.axvline(0, color="black", linewidth=1.2, zorder=2)

    txt_tr = mtransforms.blended_transform_factory(ax_txt.transAxes, ax_txt.transData)
    for y, r in zip(ys, rows):
        mark = " *" if r.get("significant") else ""
        ax_txt.text(0.0, y,
                    f"{r['avg_place']:.2f} | {100 * r['share']:5.1f}% "
                    f"| n={r['n']:<6} | top4 {100 * r['top4']:3.0f}%{mark}",
                    transform=txt_tr, va="center", ha="left", fontsize=8,
                    family="monospace")

    # losange : la place moyenne lissee, celle qui sert au classement
    ax.scatter([r["score"] for r in rows], ys, marker="D", s=26,
               facecolor="white", edgecolor="black", linewidth=0.8, zorder=5,
               label="valeur lissee (classement)")

    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Delta de place moyenne vs la comp (negatif = meilleur) — "
                  "epaisseur de barre = part de l'echantillon")
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax.grid(axis="x", linestyle="--", alpha=0.3, zorder=1)
    ax.margins(x=0.08)

    # icones dans la marge droite : la colonne de gauche reste lisible.
    # Les noms d'items varient trop d'un set a l'autre (TFT_Item_X, DA_X,
    # DA_18_EmblemX) pour etre filtres par motif : on tente le CDN et on
    # ignore ce qui n'existe pas.
    if icons:
        img_tr = mtransforms.blended_transform_factory(
            ax_img.transAxes, ax_img.transData)
        for y, r in zip(ys, rows):
            for j, item in enumerate(r["key"][:3]):
                try:
                    img = api.get_item_image(strip_copy_suffix(item)).resize((30, 30))
                except Exception:  # noqa: BLE001
                    continue
                ab = AnnotationBbox(
                    OffsetImage(img, zoom=0.62), (0.30 * j, y), xycoords=img_tr,
                    frameon=False, box_alignment=(0.0, 0.5), annotation_clip=False)
                ax_img.add_artist(ab)

    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def plot_scatter(rows: list[dict], title: str, path: str) -> None:
    """Volume joue vs performance : ou est le consensus, ou sont les niches."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    if not rows:
        return
    xs = [r["avg_place"] for r in rows]
    ys = [r["n"] for r in rows]
    span = max(0.35, max(abs(r["d_avg"]) for r in rows))
    norm = mcolors.TwoSlopeNorm(vmin=-span, vcenter=0.0, vmax=span)
    cmap = plt.get_cmap("RdYlGn_r")

    fig, ax = plt.subplots(figsize=(13, 8))
    ax.scatter(xs, ys, s=140, c=[cmap(norm(r["d_avg"])) for r in rows],
               edgecolor="k", zorder=3)
    ax.set_yscale("log")
    for x, y, r in zip(xs, ys, rows):
        ax.annotate(r["label"], (x, y), textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=7)
    ax.set_xlabel("Place moyenne")
    ax.set_ylabel("Parties (echelle log)")
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.grid(True, linestyle="--", alpha=0.3, zorder=1)
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def html_table(rows: list[dict], limit: int = 25) -> str:
    if not rows:
        return "<p><em>aucune donnee</em></p>"
    cols = ["label", "n", "avg_place", "shrunk_avg", "d_avg", "top4", "win", "z", "significant"]
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = []
    for r in rows[:limit]:
        tint = "#e8f5e9" if r["d_avg"] < 0 else "#ffebee"
        cells = "".join(
            f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols)
        body.append(f'<tr style="background:{tint}">{cells}</tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


HTML_CSS = """
body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}
table{border-collapse:collapse;width:100%;margin-bottom:1.5rem;font-size:13px}
th,td{border:1px solid #ddd;padding:4px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#f4f4f4}
img{max-width:100%;margin:1rem 0}
h2{border-bottom:2px solid #333;padding-bottom:.2rem;margin-top:2.5rem}
"""


def write_comp_html(result: dict, comp_dir: str, images: list[str]) -> None:
    parts = [f"<style>{HTML_CSS}</style>",
             f"<h1>{html.escape(result['name'])} — {html.escape(result['champ'])}</h1>",
             f"<p>{html.escape(result['set_label'])}</p>"]
    m = result.get("meta")
    if m:
        parts.append(
            f"<p><b>{m['games']}</b> parties ({100 * m['share_of_meta']:.2f}% du meta) — "
            f"place moyenne <b>{m['avg_place']}</b> "
            f"({m['d_avg_vs_meta']:+.2f} vs meta {m['meta_avg_place']}) — "
            f"top4 {100 * m['top4']:.1f}% [{100 * m['top4_ci'][0]:.1f}–{100 * m['top4_ci'][1]:.1f}%]</p>")
    for warn in result.get("warnings", []):
        parts.append(f"<p style='color:#b71c1c'>⚠ {html.escape(warn)}</p>")
    for img in images:
        parts.append(f"<img src='{html.escape(os.path.basename(img))}'>")
    for table, rows in result["tables"].items():
        parts.append(f"<h2>{table}</h2>{html_table(rows)}")
    with open(os.path.join(comp_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def comp_folder(name: str, champ: str) -> str:
    """Nom de dossier unique : plusieurs comps peuvent partager le meme `name`
    avec des carries differents (ex: 3 entrees "Graves")."""
    label = name if norm_name(champ) in norm_name(name) else f"{name} - {pretty_item(champ)}"
    return re.sub(r"[^\w\- ]", "_", label)


def write_outputs(result: dict, out_dir: str, api: ApiClient, cfg: argparse.Namespace) -> None:
    comp_dir = os.path.join(out_dir, comp_folder(result["name"], result["champ"]))
    os.makedirs(comp_dir, exist_ok=True)

    with open(os.path.join(comp_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    if result.get("error"):
        return

    images = []
    for table, rows in result["tables"].items():
        if not rows:
            continue
        # marque les lignes retenues (populaire / pepite) avant l'export CSV
        selection = top_selection(rows, cfg.top_common, cfg.top_hidden)
        write_csv(os.path.join(comp_dir, f"{table}.csv"), rows)
        if cfg.no_plots or table not in cfg.plot_tables:
            continue
        title = f"{result['name']} ({result['champ']}) — {table}"
        p1 = os.path.join(comp_dir, f"{table}.png")
        plot_ranked(selection, title, p1, api, icons=not cfg.no_icons)
        images.append(p1)
        if cfg.scatter:
            p2 = os.path.join(comp_dir, f"{table}_scatter.png")
            plot_scatter(selection, title, p2)
            images.append(p2)

    if not cfg.no_html:
        write_comp_html(result, comp_dir, images)


def write_summary(results: list[dict], out_dir: str, cfg: argparse.Namespace,
                  profile: "SetProfile | None" = None) -> list[dict]:
    """Ecrit summary.csv + index.html. Retourne les lignes (reutilisees par la
    publication pour comparer deux generations)."""
    rows = []
    for r in results:
        m = r.get("meta") or {}
        rows.append({
            "comp": comp_folder(r["name"], r["champ"]), "champ": r["champ"],
            "games": m.get("games", 0),
            "share_of_meta": round(m.get("share_of_meta", 0), 5),
            "avg_place": m.get("avg_place", ""),
            "d_avg_vs_meta": m.get("d_avg_vs_meta", ""),
            "top4": m.get("top4", ""),
            "win": m.get("win", ""),
            "erreur": r.get("error", ""),
        })
    rows.sort(key=lambda r: (r["avg_place"] == "", r["avg_place"]))
    path = os.path.join(out_dir, "summary.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["comp"])
        w.writeheader()
        w.writerows(rows)

    if cfg.no_html or not rows:
        return rows

    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    subtitle = profile.label if profile else ""
    header = "".join(f"<th>{html.escape(c)}</th>" for c in rows[0])
    body = ["<meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            f"<title>TFT — {html.escape(subtitle)}</title>",
            "<style>" + HTML_CSS + "</style>",
            "<h1>Comps analysees</h1>",
            f"<p>{html.escape(subtitle)} — genere le {generated} — "
            f"{len([r for r in rows if not r['erreur']])} comps</p>",
            f"<table><thead><tr>{header}</tr></thead><tbody>"]
    for r in rows:
        # les dossiers contiennent espaces et accents : encoder l'URL, sinon
        # les liens cassent une fois servis par GitHub Pages
        href = quote(f"{r['comp']}/report.html")
        link = f"<a href='{href}'>{html.escape(r['comp'])}</a>"
        cells = [link] + [html.escape(str(v)) for v in list(r.values())[1:]]
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    body.append("</tbody></table>")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    print(f"Rapport global : {os.path.join(out_dir, 'index.html')}")
    return rows


# --------------------------------------------------------------------------- #
# Modes utilitaires
# --------------------------------------------------------------------------- #

def cmd_validate(comps: list[dict], profile: SetProfile) -> int:
    """Dit quelles comps survivent au set cible : indispensable le jour d'un
    changement de set (les unites et traits disparus sont listes)."""
    print(f"Set cible : {profile.label}, {len(profile.units)} unites connues\n")
    broken = 0
    for comp in comps:
        problems = comp_problems(comp, profile)
        if problems:
            broken += 1
            print(f"  KO  {comp['name']:<32} " + " | ".join(problems))
        else:
            print(f"  OK  {comp['name']:<32} {profile.resolve_unit(comp['champ'])}")
    print(f"\n{broken} comp(s) a reecrire pour {profile.label}.")
    return broken


def cmd_discover(api: ApiClient, base_params: list[tuple[str, str]],
                 profile: SetProfile, top: int, path: str) -> None:
    """Genere un comps.json de depart pour un set : les carries les plus joues."""
    # Ce qui distingue un carry d'une invocation n'est pas son nom (le Set 18
    # rend jouables Sentry, Sentinel, Krug, Scuttlecrab, Elder Dragon...) mais
    # le fait de pouvoir porter des items : on lit donc la table unit_items.
    holders: set[str] = set()
    for row in api.get_json("unit_items", base_params).get("data", []):
        pair = row.get("unit_items")
        if pair:
            holders.add(pair.split("&", 1)[0])

    rows = []
    for row in api.get_json("units", base_params).get("data", []):
        unit = row.get("units")
        if not unit or unit not in profile.units:
            continue
        if holders and unit not in holders:
            continue
        st = Stats.from_counts(row["placement_count"])
        rows.append((unit, st))
    rows.sort(key=lambda t: t[1].n, reverse=True)

    comps = []
    for unit, st in rows[:top]:
        query = urlencode([("tab", "items"), ("unit", f"{unit}-1_.*_3")], safe=".*!,|-")
        comps.append({
            "name": pretty_item(unit),
            "champ": unit,
            "explorer_url": f"https://www.metatft.com/explorer?{query}",
            "_note": f"{st.n} parties, place moyenne {st.avg:.2f}",
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(comps, f, ensure_ascii=False, indent=2)
    print(f"{len(comps)} comps generees dans {path} — a affiner dans l'explorer MetaTFT.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--comps", default="comps.json")
    p.add_argument("--output", default="output")
    p.add_argument("--sets-file", default="sets.json")
    p.add_argument("--set", default=None,
                   help="numero de set (ex: 18). Par defaut : set live detecte")
    p.add_argument("--queue", default="1100")
    p.add_argument("--patch", default="current")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--ranks", default="CHALLENGER,GRANDMASTER,MASTER,DIAMOND,EMERALD")
    p.add_argument("--min-games", type=int, default=40,
                   help="echantillon minimum pour qu'une ligne soit gardee")
    p.add_argument("--prior", type=float, default=30.0,
                   help="plancher du lissage bayesien, en parties")
    p.add_argument("--prior-frac", type=float, default=0.02,
                   help="force du lissage en fraction de l'echantillon de la "
                        "comp (0.02 = un build a 2%% de part perd la moitie de "
                        "son ecart). 0 = ancien comportement")
    p.add_argument("--alpha", type=float, default=0.10, help="seuil FDR")
    p.add_argument("--build-size", type=int, default=3)
    p.add_argument("--top-common", type=int, default=10)
    p.add_argument("--top-hidden", type=int, default=6)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--cache-ttl", type=float, default=3600.0)
    p.add_argument("--cache-dir", default=".cache")
    p.add_argument("--plot-tables", default="builds,item_lift,items_radiant,items_artifact,items_mechanic,etoiles")
    p.add_argument("--scatter", action="store_true", help="ajoute le nuage volume/perf")
    p.add_argument("--flex", action="store_true", default=True,
                   help="analyse des unites et traits flex (defaut: actif)")
    p.add_argument("--no-flex", dest="flex", action="store_false")
    p.add_argument("--no-plots", action="store_true")
    p.add_argument("--no-icons", action="store_true")
    p.add_argument("--no-html", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="analyse quand meme les comps incompatibles avec le set "
                        "cible (les filtres disparus sont alors sans effet)")
    pub = p.add_argument_group("publication (cf. publish.py)")
    pub.add_argument("--publish", action="store_true",
                     help="apres la generation : diff avec la derniere execution, "
                          "deploiement et/ou notification Discord")
    pub.add_argument("--deploy", action="store_true",
                     help="avec --publish : copie dans docs/ puis commit + push")
    pub.add_argument("--notify", action="store_true",
                     help="avec --publish : poste les mouvements sur Discord")
    pub.add_argument("--publish-dry-run", action="store_true",
                     help="avec --publish : simule sans rien ecrire ni envoyer")
    pub.add_argument("--site-url", default="",
                     help="URL publique du site (sinon $SITE_URL)")
    pub.add_argument("--threshold", type=float, default=0.10,
                     help="ecart de place moyenne considere comme notable")

    p.add_argument("--validate", action="store_true",
                   help="verifie la compatibilite des comps avec le set cible")
    p.add_argument("--discover", type=int, metavar="N",
                   help="genere un comps.json avec les N carries les plus joues")
    p.add_argument("-v", "--verbose", action="store_true")
    cfg = p.parse_args(argv)
    cfg.plot_tables = {t.strip() for t in cfg.plot_tables.split(",") if t.strip()}
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    cfg = parse_args(argv)
    api = ApiClient(cfg.cache_dir, ttl=cfg.cache_ttl, verbose=cfg.verbose)

    base_params: list[tuple[str, str]] = [
        ("formatnoarray", "true"), ("compact", "true"),
        ("queue", cfg.queue), ("patch", cfg.patch), ("days", str(cfg.days)),
        ("rank", cfg.ranks), ("permit_filter_adjustment", "true"),
    ]

    profiles = load_set_profiles(cfg.sets_file)
    profile = resolve_set(api, base_params, profiles, cfg.set)
    # le profil peut imposer sa propre queue / son propre patch (ex: PBE)
    base_params = profile.params
    filters_used = " ".join(f"{k}={v}" for k, v in base_params
                            if k in ("queue", "patch", "days", "rank", "set") and v)
    print(f"Set analyse : {profile.label} — {len(profile.units)} unites, "
          f"{len(profile.traits)} traits, {len(profile.items)} items")
    print(f"Filtres     : {filters_used}")

    if not profile.units:
        print("⚠ Aucune donnee pour ce set sur cette queue. Pour un set en test, "
              "essaie --set pbe (queue=PBE).", file=sys.stderr)
        return 2

    if cfg.discover:
        slug = re.sub(r"[^\w\-]", "", profile.key)
        cmd_discover(api, base_params, profile, cfg.discover, f"comps.{slug}.json")
        return 0

    with open(cfg.comps, "r", encoding="utf-8") as f:
        comps = json.load(f)
    print(f"{len(comps)} comps chargees depuis {cfg.comps}")

    if cfg.validate:
        cmd_validate(comps, profile)
        return 0

    os.makedirs(cfg.output, exist_ok=True)

    def run(comp: dict) -> dict:
        try:
            return analyse_comp(comp, api, profile, base_params, cfg)
        except Exception as exc:  # noqa: BLE001
            return {"name": comp.get("name", "?"), "champ": comp.get("champ", "?"),
                    "set": profile.key, "set_label": profile.label,
                    "tables": {}, "error": str(exc)}

    # Requetes en parallele, rendu matplotlib en sequentiel (non thread-safe).
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        results = list(pool.map(run, comps))

    for result in results:
        if result.get("error"):
            print(f"  ✗ {result['name']}: {result['error']}")
        else:
            m = result["meta"]
            print(f"  ✓ {result['name']:<32} {m['games']:>7} parties  "
                  f"place {m['avg_place']:.2f} ({m['d_avg_vs_meta']:+.2f} vs meta)")
        write_outputs(result, cfg.output, api, cfg)

    write_summary(results, cfg.output, cfg, profile)

    if cfg.publish:
        import publish as publish_mod  # import tardif : requests seulement

        print(f"\n{'=' * 60}\nPublication")
        publish_mod.publish(
            cfg.output, site_url=cfg.site_url, set_label=profile.label,
            threshold=cfg.threshold, notify=cfg.notify, do_deploy=cfg.deploy,
            dry_run=cfg.publish_dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
