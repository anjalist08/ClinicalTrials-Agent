"""Country name -> ISO 3166-1 codes, for choropleth maps.

ClinicalTrials.gov uses its own country spellings (e.g. "Korea, Republic of",
"Turkey", "Russian Federation"); pycountry resolves most of them and the
aliases below cover the rest.
"""

from __future__ import annotations

from functools import lru_cache

import pycountry

_ALIASES = {
    "turkey": "TUR",
    "türkiye": "TUR",
    "russia": "RUS",
    "south korea": "KOR",
    "korea": "KOR",
    "north korea": "PRK",
    "iran": "IRN",
    "taiwan": "TWN",
    "vietnam": "VNM",
    "czech republic": "CZE",
    "macedonia, the former yugoslav republic of": "MKD",
    "former serbia and montenegro": "SRB",
    "congo, the democratic republic of the": "COD",
    "tanzania": "TZA",
    "bolivia": "BOL",
    "venezuela": "VEN",
    "moldova": "MDA",
    "syria": "SYR",
    "laos": "LAO",
    "palestinian territory, occupied": "PSE",
    "côte d'ivoire": "CIV",
    "cote d'ivoire": "CIV",
    "kosovo": "XKX",
}


@lru_cache(maxsize=512)
def iso_codes(name: str) -> tuple[str | None, str | None]:
    """Return ``(alpha_3, numeric)``; numeric matches world-atlas TopoJSON feature ids."""
    key = name.strip().lower()
    alpha3 = _ALIASES.get(key)
    country = None
    try:
        country = pycountry.countries.get(alpha_3=alpha3) if alpha3 else pycountry.countries.lookup(name)
    except LookupError:
        try:
            matches = pycountry.countries.search_fuzzy(name)
            country = matches[0] if matches else None
        except LookupError:
            country = None
    if country is None:
        return alpha3, None
    return country.alpha_3, getattr(country, "numeric", None)
