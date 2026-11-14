import pytest

from keep_to_bring.parsing import ParsedItem, clean_text, parse_quantity, split_entries


@pytest.mark.parametrize(
    ("text", "name", "spec"),
    [
        ("milk", "milk", ""),
        ("2 gallons of milk", "milk", "2 gallons"),
        ("1 gallon milk", "milk", "1 gallon"),
        ("a dozen eggs", "eggs", "1 dozen"),
        ("half a dozen eggs", "eggs", "1/2 dozen"),
        ("three apples", "apples", "3"),
        ("one onion", "onion", "1"),
        ("3 apples", "apples", "3"),
        ("500g flour", "flour", "500 g"),
        ("1.5 kg potatoes", "potatoes", "1.5 kg"),
        ("1 1/2 lbs ground beef", "ground beef", "1 1/2 lbs"),
        ("a bag of chips", "chips", "1 bag"),
        ("an onion", "an onion", ""),  # "an" alone is not a quantity
        ("2 cans of tomatoes", "tomatoes", "2 cans"),
        ("2 six packs of soda", "soda", "2 six packs"),
        ("2x butter", "butter", "2"),
        ("2 x butter", "butter", "2"),
        ("milk x2", "milk", "2"),
        ("eggs (12)", "eggs", "12"),
        ("bananas (organic)", "bananas", "organic"),
        # Things that must NOT be treated as quantities:
        ("2% milk", "2% milk", ""),
        ("2 percent milk", "2 percent milk", ""),
        ("7 up", "7 up", ""),
        ("7 grain bread", "7 grain bread", ""),
        ("apt", "apt", ""),
        ("AA batteries", "AA batteries", ""),
        ("V8", "V8", ""),
        ("half and half", "half and half", ""),
    ],
)
def test_parse_quantity(text, name, spec):
    assert parse_quantity(text) == ParsedItem(name, spec)


def test_clean_text():
    assert clean_text("  - milk.  ") == "milk"
    assert clean_text("☐ eggs") == "eggs"
    assert clean_text("paper   towels!") == "paper towels"


def test_split_entries_lines_always_split():
    assert split_entries("milk\n\neggs", split_items=False) == ["milk", "eggs"]


def test_split_entries_disabled_keeps_phrase():
    assert split_entries("milk, eggs and bread", split_items=False) == ["milk, eggs and bread"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("milk, eggs and bread", ["milk", "eggs", "bread"]),
        ("salt, pepper, and oil", ["salt", "pepper", "oil"]),
        ("apples & pears", ["apples", "pears"]),
        ("butter plus jam", ["butter", "jam"]),
        ("half and half", ["half and half"]),
        ("mac and cheese", ["mac and cheese"]),
        ("2 boxes of mac and cheese", ["2 boxes of mac and cheese"]),
    ],
)
def test_split_entries_enabled(text, expected):
    assert split_entries(text, split_items=True) == expected


def test_split_entries_keeps_known_products_together():
    known = {"salt and vinegar crisps"}
    assert split_entries(
        "salt and vinegar crisps", split_items=True, is_known=lambda t: t in known
    ) == ["salt and vinegar crisps"]


def test_label():
    assert ParsedItem("Milk", "2 gallons").label() == "Milk (2 gallons)"
    assert ParsedItem("Milk").label() == "Milk"


@pytest.mark.parametrize(
    ("text", "name", "spec"),
    [
        # Numbers that describe the product, not how many to buy.
        ("9 volt batteries", "batteries", "9 volt"),
        ("40 watt bulbs", "bulbs", "40 watt"),
        ("9 inch pie crust", "pie crust", "9 inch"),
        ("3 ply toilet paper", "toilet paper", "3 ply"),
        ("100 count paper plates", "paper plates", "100 count"),
        ("one a day vitamins", "one a day vitamins", ""),
        ("five spice", "five spice", ""),
        ("8 o'clock coffee", "8 o'clock coffee", ""),
        # A unit word that is really part of the product name.
        ("a can opener", "a can opener", ""),
        ("a bottle opener", "a bottle opener", ""),
        ("a box cutter", "a box cutter", ""),
        ("a tin foil", "a tin foil", ""),
        ("2 can openers", "can openers", "2"),  # two can openers, not "openers (2 can)"
        # Articles still work as amounts with "of", and "a dozen" without.
        ("a can of soup", "soup", "1 can"),
        ("a dozen eggs", "eggs", "1 dozen"),
        # Decimal commas.
        ("1,5 kg pork belly", "pork belly", "1,5 kg"),
    ],
)
def test_review_regressions(text, name, spec):
    assert parse_quantity(text) == ParsedItem(name, spec)


def test_decimal_comma_is_not_split():
    assert split_entries("1,5 kg pork belly", split_items=True) == ["1,5 kg pork belly"]
    assert split_entries("0,5 l oat cream, eggs", split_items=True) == ["0,5 l oat cream", "eggs"]
