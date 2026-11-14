import pytest

from keep_to_bring.catalog import Catalog


@pytest.fixture(scope="module")
def en_us() -> Catalog:
    return Catalog.for_locale("en-US")


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("milk", "Milk"),
        ("MILK", "Milk"),
        ("eggs", "Eggs"),
        ("egg", "Eggs"),
        ("apple", "Apples"),
        ("tomato", "Tomatoes"),
        ("strawberry", "Strawberries"),
        ("paper towels", "Paper towels"),
        ("paper towel", "Paper towels"),
        ("toilet-paper", "Toilet paper"),
        ("popcorn", "Pop corn"),
        ("some bread", "Bread"),
        ("more coffee", "Coffee"),
    ],
)
def test_matches_catalogue(en_us, spoken, expected):
    assert en_us.match(spoken) == expected
    assert en_us.display_name(spoken) == expected


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("rex chew toy", "Rex chew toy"),  # lowercase -> capitalised
        ("iPhone charger", "iPhone charger"),  # mixed case kept as-is
        ("AA batteries", "AA batteries"),
        ("  spaced   out  ", "Spaced out"),
    ],
)
def test_custom_items(en_us, spoken, expected):
    assert en_us.match(spoken) is None
    assert en_us.display_name(spoken) == expected


def test_catalogue_is_loaded(en_us):
    assert len(en_us) > 300
    assert en_us.locale == "en-US"


def test_german_locale_matches_case_insensitively():
    catalog = Catalog.for_locale("de-DE")
    assert catalog.match("milch") == "Milch"
    # English plural rules are not applied to other languages.
    assert catalog.match("milchs") is None


def test_key_locale_uses_catalogue_keys():
    catalog = Catalog.for_locale("de-CH")
    assert catalog.match("milch") == "Milch"


def test_unknown_locale_gives_empty_catalogue():
    catalog = Catalog.for_locale("xx-XX")
    assert len(catalog) == 0
    assert catalog.display_name("milk") == "Milk"


def test_empty_catalogue():
    assert Catalog.empty().match("milk") is None


def test_custom_items_drop_leading_article(en_us):
    assert en_us.display_name("a tin foil") == "Tin foil"
    assert en_us.display_name("some birthday candles") == "Birthday candles"
    assert en_us.display_name("a") == "A"
