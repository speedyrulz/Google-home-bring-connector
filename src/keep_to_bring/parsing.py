"""Turn the raw text of a Keep list item into clean shopping-list entries.

Examples (with quantity parsing on)::

    "2 gallons of milk"   -> name "milk",   spec "2 gallons"
    "a dozen eggs"        -> name "eggs",   spec "1 dozen"
    "three apples"        -> name "apples", spec "3"
    "bananas (organic)"   -> name "bananas", spec "organic"
    "2% milk"             -> name "2% milk" (left alone)

The parser is deliberately conservative: when in doubt it leaves the text
as-is, because a slightly odd item name is better than a mangled one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedItem:
    name: str
    spec: str = ""

    def label(self) -> str:
        return f"{self.name} ({self.spec})" if self.spec else self.name


_WHITESPACE = re.compile(r"\s+")
_LEADING_JUNK = re.compile(r"^[\s\-–—•*·☐☑✓✔]+")
_TRAILING_JUNK = re.compile(r"[\s.!?,;:]+$")


def clean_text(text: str) -> str:
    """Strip bullets, trailing punctuation and repeated whitespace."""
    text = _LEADING_JUNK.sub("", text)
    text = _TRAILING_JUNK.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


# --------------------------------------------------------------------------
# Splitting "milk, eggs and bread" into separate items (opt-in)
# --------------------------------------------------------------------------

_SPLIT = re.compile(r"\s*[,;]\s*(?:and\s+|&\s*)?|\s+(?:and|&|plus)\s+", re.IGNORECASE)

# Common products whose names contain "and"/"&" and must never be split.
_UNSPLITTABLE = {
    "half and half", "half & half", "mac and cheese", "mac & cheese", "macaroni and cheese",
    "salt and pepper", "salt & pepper", "sweet and sour sauce", "fish and chips",
    "m&ms", "m&m's", "m & ms", "ben and jerry's", "ben & jerry's", "pork and beans",
    "salt and vinegar chips", "salt & vinegar chips", "peanut butter and jelly",
    "chips and salsa", "chips & salsa", "cookies and cream", "cookies & cream",
    "head and shoulders", "head & shoulders", "johnson and johnson", "johnson & johnson",
    "arm and hammer", "arm & hammer", "black and decker", "bread and butter pickles",
}


def split_entries(
    text: str,
    *,
    split_items: bool,
    is_known: Callable[[str], bool] = lambda _text: False,
) -> list[str]:
    """Split one Keep item into one or more shopping-list entries.

    Separate lines are always separate items. When ``split_items`` is on,
    commas, semicolons and the words "and"/"plus" also separate items, unless
    ``is_known`` says the whole phrase is a single known product (so that
    e.g. a catalogue item like "salt and pepper" would stay together).
    """
    entries: list[str] = []
    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        if split_items and not _is_unsplittable(line) and not is_known(line):
            parts = [clean_text(part) for part in _SPLIT.split(line)]
            parts = [part for part in parts if part]
            entries.extend(parts or [line])
        else:
            entries.append(line)
    return entries


def _is_unsplittable(line: str) -> bool:
    lowered = _WHITESPACE.sub(" ", line.casefold())
    return any(phrase in lowered for phrase in _UNSPLITTABLE)


# --------------------------------------------------------------------------
# Quantities: "2 lbs of chicken" -> ("chicken", "2 lbs")
# --------------------------------------------------------------------------

_UNITS = sorted(
    {
        "lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces",
        "g", "gram", "grams", "kg", "kgs", "kilo", "kilos", "kilogram", "kilograms",
        "l", "liter", "liters", "litre", "litres", "ml",
        "gal", "gallon", "gallons", "half gallon", "half gallons",
        "qt", "quart", "quarts", "pt", "pint", "pints", "cup", "cups",
        "can", "cans", "tin", "tins", "jar", "jars", "jug", "jugs",
        "bottle", "bottles", "box", "boxes", "bag", "bags", "sack", "sacks",
        "pack", "packs", "package", "packages", "packet", "packets", "pkg",
        "six pack", "six packs", "six-pack", "six-packs", "case", "cases",
        "carton", "cartons", "container", "containers", "tub", "tubs", "tray", "trays",
        "bunch", "bunches", "head", "heads", "loaf", "loaves",
        "stick", "sticks", "roll", "rolls", "slice", "slices", "piece", "pieces",
        "pc", "pcs", "block", "blocks", "dozen", "dozens",
    },
    key=len,
    reverse=True,
)
_UNIT = "(?:" + "|".join(re.escape(u).replace(r"\ ", r"\s+") for u in _UNITS) + ")"

_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
_WORD_NUMBER = "(?:" + "|".join(_NUMBER_WORDS) + ")"
_DIGITS = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?[½¼¾]?|[½¼¾⅓⅔])"

# Number + unit: "2 gallons of milk", "500g flour", "a bag of chips", "half a dozen eggs".
# Digits may touch the unit ("500g"); words need a space ("a pack", not "apack").
_WITH_UNIT = re.compile(
    rf"^(?:(?P<dqty>{_DIGITS})\s*-?\s*|(?P<wqty>{_WORD_NUMBER}|half(?:\s+an?)?|an?)\s+)"
    rf"(?P<unit>{_UNIT})\.?\s+(?:of\s+)?(?P<name>.+)$",
    re.IGNORECASE,
)
# Bare number: "3 apples", "two onions", "2x milk", "2 x milk"
_BARE_NUMBER = re.compile(
    rf"^(?P<qty>{_DIGITS}|{_WORD_NUMBER})\s*(?:x\s+|x(?=\s)|\s)\s*(?P<name>.+)$",
    re.IGNORECASE,
)
# Trailing forms: "milk x2", "milk x 2", "eggs (12)", "bananas (organic)"
_TRAILING = re.compile(
    rf"^(?P<name>.+?)\s*(?:\bx\s*(?P<qty>{_DIGITS})|\((?P<paren>[^()]+)\))$",
    re.IGNORECASE,
)

# Words that commonly follow a number as part of a product name, where the
# number is not a quantity ("7 up", "2 percent milk", "7 grain bread").
_NOT_A_QUANTITY_NEXT_WORDS = {
    "percent", "%", "up", "grain", "grains", "hour", "lives", "layer", "musketeers",
    "bean", "alarm", "minute", "day", "in", "year", "way", "star", "seas",
}


def _normalise_qty(qty: str) -> str:
    qty = _WHITESPACE.sub(" ", qty.strip().lower())
    if qty in _NUMBER_WORDS:
        return _NUMBER_WORDS[qty]
    if qty in {"a", "an"}:
        return "1"
    if qty.startswith("half"):
        return "1/2"
    return qty


def parse_quantity(text: str) -> ParsedItem:
    """Split a leading/trailing quantity off an item, e.g. "2 lbs chicken"."""
    text = clean_text(text)

    match = _WITH_UNIT.match(text)
    if match and _plausible_name(match.group("name")):
        unit = _WHITESPACE.sub(" ", match.group("unit").lower())
        qty = match.group("dqty") or match.group("wqty")
        spec = f"{_normalise_qty(qty)} {unit}"
        return ParsedItem(clean_text(match.group("name")), spec)

    match = _BARE_NUMBER.match(text)
    if match and _plausible_name(match.group("name")):
        first_word = match.group("name").split()[0].lower()
        if first_word not in _NOT_A_QUANTITY_NEXT_WORDS:
            return ParsedItem(clean_text(match.group("name")), _normalise_qty(match.group("qty")))

    match = _TRAILING.match(text)
    if match and _plausible_name(match.group("name")):
        spec = match.group("qty") or match.group("paren") or ""
        return ParsedItem(clean_text(match.group("name")), clean_text(spec))

    return ParsedItem(text)


def _plausible_name(name: str) -> bool:
    return sum(ch.isalpha() for ch in name) >= 3
