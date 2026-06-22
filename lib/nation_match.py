"""Cross-source nation name → canonical nation_id resolver.

Built once from wc26_nations after Notebook 01 runs. Every downstream notebook
just calls match_to_canonical("South Korea") or match_to_canonical("KOR").
"""
from __future__ import annotations
import re
from functools import lru_cache

import pandas as pd

from .io import load_table


def _norm(s: str) -> str:
    s = s.lower().strip()
    # collapse accents/punct/whitespace
    s = re.sub(r"[à-æ]", "a", s)
    s = re.sub(r"[è-ë]", "e", s)
    s = re.sub(r"[ì-ï]", "i", s)
    s = re.sub(r"[ò-öø]", "o", s)
    s = re.sub(r"[ù-ü]", "u", s)
    s = re.sub(r"[ç]", "c", s)
    s = re.sub(r"[ñ]", "n", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, str]:
    df = load_table("wc26_nations")
    idx: dict[str, str] = {}
    name_cols = [c for c in df.columns if c.endswith("_name") or c == "name"]
    for _, row in df.iterrows():
        nid = row["nation_id"]
        idx[_norm(nid)] = nid
        for col in name_cols:
            v = row.get(col)
            if isinstance(v, str) and v:
                idx[_norm(v)] = nid
        for col in ("all_names", "all_aliases"):
            v = row.get(col)
            # Parquet rehydrates list cols as numpy.ndarray; CSV as repr-strings.
            # Accept any non-string iterable.
            if v is None or isinstance(v, str):
                continue
            try:
                for alias in v:
                    if isinstance(alias, str) and alias:
                        idx[_norm(alias)] = nid
            except TypeError:
                continue
    return idx


def match_to_canonical(name_or_code: str) -> str | None:
    if not name_or_code:
        return None
    return _alias_index().get(_norm(name_or_code))
