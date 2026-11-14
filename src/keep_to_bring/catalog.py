"""Match spoken item names against the Bring! item catalogue.

Bring! shows a nice icon and sorts an item into the right aisle only when the
item name exactly matches one of its catalogue entries (e.g. "Milk"). Voice
input usually arrives in Keep as lowercase ("milk") or in the singular
("apple" vs. the catalogue's "Apples"), which Bring! would otherwise store as
a plain custom item. This module finds the matching catalogue name.

The catalogue files ship with the ``bring-api`` package (one JSON file per
locale, mapping Bring's internal Swiss-German keys to localised names).
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Iterable, Iterator
from functools import lru_cache
from importlib import resources

_LOGGER = logging.getLogger(__name__)

# Bring's internal catalogue keys are Swiss German; that locale needs no file.
BRING_KEY_LOCALE = "de-CH"
_ENGLISH_FILLERS = ("some ", "more ", "a ", "an ", "the ")
# Dropped from custom (non-catalogue) item names: "a tin foil" -> "Tin foil".
_CUSTOM_ITEM_FILLERS = ("some ", "more ", "a ", "an ")
_VOWELS = set("aeiou")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.replace("-", " ")
    return " ".join(text.split())


def _squash(text: str) -> str:
    return _norm(text).replace(" ", "")


@lru_cache(maxsize=None)
def _load_locale(locale: str) -> dict[str, str]:
    path = resources.files("bring_api") / "locales" / f"articles.{locale}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        _LOGGER.warning("No Bring! catalogue available for locale %s; using names as spoken.", locale)
        return {}


def _english_variants(phrase: str) -> Iterator[str]:
    """Singular/plural guesses for the last word of an English phrase."""
    if phrase.endswith("ies") and len(phrase) > 4:
        yield phrase[:-3] + "y"  # berries -> berry
    if phrase.endswith("ves") and len(phrase) > 4:
        yield phrase[:-3] + "f"  # loaves -> loaf
        yield phrase[:-3] + "fe"  # knives -> knife
    if phrase.endswith("es") and len(phrase) > 3:
        yield phrase[:-2]  # tomatoes -> tomato, boxes -> box
    if phrase.endswith("s") and not phrase.endswith("ss") and len(phrase) > 2:
        yield phrase[:-1]  # apples -> apple
    if phrase.endswith("y") and len(phrase) > 2 and phrase[-2] not in _VOWELS:
        yield phrase[:-1] + "ies"  # berry -> berries
    if phrase.endswith("f"):
        yield phrase[:-1] + "ves"
    if phrase.endswith("fe"):
        yield phrase[:-2] + "ves"
    if phrase.endswith(("s", "x", "z", "ch", "sh", "o")):
        yield phrase + "es"  # tomato -> tomatoes
    yield phrase + "s"  # apple -> apples


class Catalog:
    """Case-insensitive lookup of catalogue names for one Bring! list locale."""

    def __init__(self, names: Iterable[str], *, locale: str = "en-US") -> None:
        self.locale = locale
        self._english = locale.lower().startswith("en")
        self._exact: dict[str, str] = {}
        self._squashed: dict[str, str] = {}
        for name in names:
            self._exact.setdefault(_norm(name), name)
            self._squashed.setdefault(_squash(name), name)

    @classmethod
    def for_locale(cls, locale: str) -> Catalog:
        if locale == BRING_KEY_LOCALE:
            # In the key locale the display name *is* the key. Any locale file
            # lists all keys; de-DE is always shipped.
            return cls(_load_locale("de-DE").keys(), locale=locale)
        return cls(_load_locale(locale).values(), locale=locale)

    @classmethod
    def empty(cls) -> Catalog:
        return cls((), locale="")

    def __len__(self) -> int:
        return len(self._exact)

    def _candidates(self, name: str) -> Iterator[str]:
        base = _norm(name)
        yield base
        bases = [base]
        if self._english:
            for filler in _ENGLISH_FILLERS:
                if base.startswith(filler) and len(base) > len(filler):
                    stripped = base[len(filler):]
                    bases.append(stripped)
                    yield stripped
            for candidate in bases:
                yield from _english_variants(candidate)

    def match(self, name: str) -> str | None:
        """The catalogue spelling of ``name``, or ``None`` if it isn't in the catalogue."""
        if not self._exact or not name.strip():
            return None
        for candidate in self._candidates(name):
            if candidate in self._exact:
                return self._exact[candidate]
            squashed = candidate.replace(" ", "")
            if squashed in self._squashed:
                return self._squashed[squashed]
        return None

    def display_name(self, name: str) -> str:
        """Catalogue name if known; otherwise the spoken name, capitalised if all-lowercase."""
        name = " ".join(name.split())
        matched = self.match(name)
        if matched:
            return matched
        if self._english:
            for filler in _CUSTOM_ITEM_FILLERS:
                if name.lower().startswith(filler) and len(name) > len(filler):
                    name = name[len(filler):]
                    break
        if name.islower():
            return name[:1].upper() + name[1:]
        return name
