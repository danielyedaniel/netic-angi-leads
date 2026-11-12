"""Value helpers: contact normalisation, timestamps, money and flags.

Covers app/util.py and the small value helpers at the top of app/angi/parser.py.
Everything here is a pure function, so no fixtures are needed.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.angi.parser import json_type, to_bool, to_cents
from app.util import (
    clean_str,
    dumps,
    format_cents,
    iso,
    loads,
    new_id,
    new_token,
    normalize_email,
    normalize_phone,
    one_line,
    parse_iso,
    seconds_between,
)

# --- normalize_phone ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "(555) 433-2646",
        "555.433.2646",
        "555-433-2646",
        "555 433 2646",
        "  5554332646  ",
        "+1 555 433 2646",
        "+15554332646",
        "15554332646",
        "1-555-433-2646",
        "1 (555) 433-2646",
        "(555) 433-2646\n",
        5554332646,
        15554332646,
    ],
)
def test_phone_formats_of_one_number_normalise_to_the_same_e164(raw):
    # Duplicate detection compares phones, so every way of writing a number must give one value.
    assert normalize_phone(raw) == "+15554332646"


@pytest.mark.parametrize(
    "raw",
    [
        "555433264",  # 9 digits
        "55543326461",  # 11 digits not starting with the country code 1
        "25554332646",  # 11 digits with a wrong country code
        "115554332646",  # 12 digits
        "5554332646 5554332646",  # two numbers in one field
        "433-2646",  # no area code
        "+44 20 7946 0958",  # not North American
        "011 555 433 2646",  # international dialling prefix
    ],
)
def test_phone_with_the_wrong_number_of_digits_is_rejected(raw):
    # Only a 10-digit NANP number (optionally with a leading 1) is a usable phone.
    assert normalize_phone(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "0554332646",  # area code starts with 0
        "1554332646",  # area code starts with 1
        "5550332646",  # exchange starts with 0
        "5551332646",  # exchange starts with 1
        "10554332646",  # same again behind a country code
        "11554332646",
        "15550332646",
        "15551332646",
        "0000000000",
        "1111111111",
    ],
)
def test_phone_whose_area_code_or_exchange_starts_with_0_or_1_is_rejected(raw):
    # NANP never assigns those, so such a value is junk and must not match other junk.
    assert normalize_phone(raw) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "call me", "n/a", "-", "+", "()", True, False])
def test_phone_that_is_missing_or_not_a_number_is_none(raw):
    # No phone is None, never an empty string or a partial value.
    assert normalize_phone(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "555-433-2646 x123",
        "555-433-2646x123",
        "(555) 433-2646 ext. 12",
        "555.433.2646 EXT 9",
        "555-433-2646 extension 4",
        "+1 555 433 2646 #12",
        "1-555-433-2646 x 123456",
    ],
)
def test_phone_followed_by_an_extension_is_the_number_without_it(raw):
    # The extension's digits are not part of the number. Left in, a perfectly good number
    # would be thrown away and the lead could not be matched to the consumer's other leads.
    assert normalize_phone(raw) == "+15554332646"


@pytest.mark.parametrize(
    "raw",
    [
        "555-433-264 x6",  # nine digits: the extension does not complete the number
        "x123",
        "ext. 12",
        "555-433-2646 x1234567",  # too long to be an extension: two numbers run together
        "555-433-2646 123",  # extra digits with no extension marker
    ],
)
def test_extension_handling_never_turns_junk_into_a_number(raw):
    # Only a complete number followed by a marked extension is accepted.
    assert normalize_phone(raw) is None


def test_normalised_phone_is_stable_when_normalised_again():
    # Stored values are re-used as match keys, so normalising twice must change nothing.
    once = normalize_phone("(555) 433-2646")
    assert normalize_phone(once) == once


def test_phone_result_is_always_plus_one_and_ten_ascii_digits():
    # E.164 for North America: "+1" followed by exactly ten digits.
    rng = random.Random(20261112)
    produced = 0
    for _ in range(2000):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.choice([9, 10, 10, 10, 11, 12])))
        result = normalize_phone(digits)
        if result is None:
            continue
        produced += 1
        assert len(result) == 12
        assert result.startswith("+1")
        assert result[2:].isascii() and result[2:].isdigit()
        assert result[2] not in "01" and result[5] not in "01"
    assert produced > 100  # the loop really exercised the accepting path


@pytest.mark.parametrize("raw", ["٥٥٥٤٣٣٢٦٤٦", "５５５４３３２６４６"])
def test_phone_written_with_non_ascii_digits_never_yields_a_non_e164_value(raw):
    # Arabic-Indic / full-width digits must be converted or rejected, not stored as "+1٥٥٥...".
    assert normalize_phone(raw) in (None, "+15554332646")


# --- normalize_email ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bob.builder@gmail.com", "bob.builder@gmail.com"),
        ("Bob.Builder@Gmail.COM", "bob.builder@gmail.com"),
        ("BOB@EXAMPLE.COM", "bob@example.com"),
        ("  bob@example.com  ", "bob@example.com"),
        ("\tbob@example.com\n", "bob@example.com"),
        ("Bob+Angi@Example.com", "bob+angi@example.com"),
        ("o'brien@example.com", "o'brien@example.com"),
        ("first.last@sub.example.co.uk", "first.last@sub.example.co.uk"),
    ],
)
def test_email_is_trimmed_and_lower_cased(raw, expected):
    # The same mailbox written with different casing or padding is one consumer.
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "bob.example.com",  # missing @
        "bob@",  # missing domain
        "@example.com",  # missing local part
        "bob@example",  # no top-level domain
        "bob@@example.com",
        "bob smith@example.com",
        "bob@exa mple.com",
        "Bob Builder <bob@example.com>",  # display-name form
        "bob@example.com, jane@example.com",  # two addresses
        "bob@example.com;jane@example.com",
        "bob..builder@example.com",
        ".bob@example.com",
        "bob@-example.com",
        "bob@example..com",
        "bob@example.com.",
        "mailto:bob@example.com",
        "bob@example.com\x00",
        "bob\r\nBcc: victim@example.com@example.com",  # header injection attempt
        "x" * 65 + "@example.com@",
        "not an email",
    ],
)
def test_email_that_is_not_one_valid_address_is_none(raw):
    # We only ever email a syntactically valid single address.
    assert normalize_email(raw) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
def test_email_missing_or_blank_is_none(raw):
    # Lead sources send null, "" and " " interchangeably for "no email".
    assert normalize_email(raw) is None


@pytest.mark.parametrize("raw", [5, 5.5, True, False, ["bob@example.com"], {"email": "bob@example.com"}])
def test_email_given_as_a_non_string_is_none(raw):
    # A number, flag, list or object in the email field is never an address.
    assert normalize_email(raw) is None


@pytest.mark.parametrize("raw", ["bob@example.test", "bob@example.invalid", "bob@localhost", "bob@host.localhost"])
def test_email_at_a_reserved_test_domain_is_treated_as_unusable(raw):
    # Reserved domains (.test, .invalid, localhost) can never receive mail, so no intro is sent.
    assert normalize_email(raw) is None


def test_normalised_email_is_stable_when_normalised_again():
    # Stored values are re-used as match keys, so normalising twice must change nothing.
    once = normalize_email("  Bob.Builder@Gmail.COM ")
    assert normalize_email(once) == once


# --- clean_str / one_line ------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", " ", "   ", "\t", "\n", "\r\n", " \t\n "])
def test_clean_str_turns_missing_and_blank_into_none(raw):
    # null, "" and whitespace all mean "not provided".
    assert clean_str(raw) is None


@pytest.mark.parametrize("raw", [{}, {"a": 1}, [], ["a"], [[]]])
def test_clean_str_never_stringifies_objects_or_arrays(raw):
    # A nested structure in a text field must not be stored as its Python repr.
    assert clean_str(raw) is None


def test_clean_str_trims_but_keeps_inner_whitespace():
    # Only the padding goes; the text itself is stored as sent.
    assert clean_str("  Bob  Builder \n") == "Bob  Builder"
    assert clean_str("line one\nline two") == "line one\nline two"


def test_clean_str_stringifies_numbers():
    # Ids and codes sometimes arrive as JSON numbers.
    assert clean_str(46203) == "46203"
    assert clean_str(0) == "0"
    assert clean_str(1.5) == "1.5"


def test_clean_str_caps_the_length_after_trimming():
    # A cap protects the database from absurd values; padding must not eat into it.
    assert clean_str("abcdef", 3) == "abc"
    assert clean_str("   abcdef   ", 3) == "abc"
    assert len(clean_str("x" * 5000)) == 2000
    assert clean_str("x" * 2000) == "x" * 2000


def test_clean_str_keeps_unicode_and_emoji():
    # Names are not ASCII.
    assert clean_str(" José Ünal 😀 ") == "José Ünal 😀"


def test_clean_str_removes_what_cannot_be_stored_or_shown():
    # Half an emoji (what a truncating sender leaves behind) cannot be written to SQLite and
    # would cost the whole lead; control characters have no business in a name.
    assert clean_str("Deep clean \ud83d") == "Deep clean \ufffd"
    assert clean_str("Bob\x00 Builder\x07\x1f\x7f") == "Bob Builder"
    assert clean_str("tab\tand\nnewline stay") == "tab\tand\nnewline stay"
    assert clean_str("\x00\x01") is None


def test_one_line_collapses_line_breaks_and_tabs():
    # Untrusted text used in an email subject must not be able to start a new header line.
    assert one_line("House\r\nCleaning") == "House Cleaning"
    assert one_line("a\tb\nc\rd") == "a b c d"
    assert one_line("  padded   out  ") == "padded out"
    injected = one_line("Cleaning\r\nBcc: victim@example.com")
    assert "\r" not in injected and "\n" not in injected


def test_one_line_caps_the_length():
    # Subjects stay short whatever the lead source sends.
    assert len(one_line("x" * 500)) == 120
    assert one_line("abc def ghi", 7) == "abc def"
    assert one_line("x" * 120) == "x" * 120


@pytest.mark.parametrize("raw", [None, "", "   ", "\r\n\t"])
def test_one_line_of_nothing_is_an_empty_string(raw):
    # Callers concatenate the result, so it is "" rather than None.
    assert one_line(raw) == ""


# --- timestamps ----------------------------------------------------------------------------


def test_iso_is_utc_with_millisecond_precision():
    # The storage format is "YYYY-MM-DDTHH:MM:SS.mmmZ".
    moment = datetime(2026, 11, 11, 22, 31, 5, 123456, tzinfo=UTC)
    assert iso(moment) == "2026-11-11T22:31:05.123Z"


def test_iso_converts_other_time_zones_to_utc():
    # A tenant-local time and its UTC equivalent are the same instant, so the same string.
    indianapolis = timezone(timedelta(hours=-5))
    assert iso(datetime(2026, 11, 11, 17, 31, 5, tzinfo=indianapolis)) == "2026-11-11T22:31:05.000Z"
    tokyo = timezone(timedelta(hours=9))
    assert iso(datetime(2026, 11, 12, 7, 31, 5, tzinfo=tokyo)) == "2026-11-11T22:31:05.000Z"


def test_iso_pads_every_component_to_a_fixed_width():
    # Fixed width is what makes string comparison in SQL chronological.
    samples = [
        datetime(2026, 1, 2, 3, 4, 5, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 3, 4, 5, 1_000, tzinfo=UTC),
        datetime(2026, 1, 2, 3, 4, 5, 10_000, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(2099, 6, 15, 12, 0, 0, 500, tzinfo=UTC),
    ]
    for moment in samples:
        text = iso(moment)
        assert len(text) == 24
        assert text[4] == text[7] == "-" and text[10] == "T" and text[13] == text[16] == ":"
        assert text[19] == "." and text.endswith("Z")
    assert iso(samples[0]) == "2026-01-02T03:04:05.000Z"
    assert iso(samples[1]) == "2026-01-02T03:04:05.001Z"
    assert iso(samples[2]) == "2026-01-02T03:04:05.010Z"
    assert iso(samples[3]) == "2026-12-31T23:59:59.999Z"  # truncated, never rolled into the next year


def test_iso_round_trips_through_parse_iso_to_the_millisecond():
    # What is written to the database is read back as the same instant.
    rng = random.Random(7)
    start = datetime(2000, 1, 1, tzinfo=UTC)
    for _ in range(500):
        moment = start + timedelta(microseconds=rng.randrange(0, 100 * 365 * 86_400 * 1_000_000))
        text = iso(moment)
        parsed = parse_iso(text)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert parsed == moment.replace(microsecond=moment.microsecond // 1000 * 1000)
        assert iso(parsed) == text


def test_parse_iso_accepts_offsets_and_missing_fractions():
    # Timestamps typed by a person or sent by another system are still understood.
    expected = datetime(2026, 11, 11, 22, 31, 5, tzinfo=UTC)
    assert parse_iso("2026-11-11T22:31:05Z") == expected
    assert parse_iso("2026-11-11T22:31:05.000Z") == expected
    assert parse_iso("2026-11-11T22:31:05+00:00") == expected
    assert parse_iso("2026-11-11T17:31:05-05:00") == expected


def test_parse_iso_rejects_text_that_is_not_a_timestamp():
    # A bad timestamp is an error, not a silently wrong date.
    with pytest.raises(ValueError):
        parse_iso("yesterday")


def test_iso_string_order_equals_chronological_order():
    # SQL compares these strings, so sorting them must sort the instants (sub-second ones too).
    rng = random.Random(42)
    base = datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)
    instants = [base + timedelta(milliseconds=ms) for ms in (0, 1, 2, 9, 10, 11, 99, 100, 101, 999, 1000, 1001)]
    instants += [
        datetime(2026, 12, 31, 23, 59, 59, 999_000, tzinfo=UTC),
        datetime(2027, 1, 1, 0, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 2, 28, 23, 59, 59, 999_000, tzinfo=UTC),
        datetime(2026, 3, 1, 0, 0, 0, 1_000, tzinfo=UTC),
        datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        # The same wall-clock digits in different zones must still order by instant.
        datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone(timedelta(hours=-5))),
        datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone(timedelta(hours=-7))),
        datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ]
    span_ms = 80 * 365 * 86_400 * 1000
    instants += [datetime(1990, 1, 1, tzinfo=UTC) + timedelta(milliseconds=rng.randrange(span_ms)) for _ in range(600)]
    rng.shuffle(instants)

    chronological = [iso(moment) for moment in sorted(instants)]
    assert chronological == sorted(iso(moment) for moment in instants)
    # Distinct milliseconds give distinct strings, so nothing collapses.
    assert len(set(chronological)) == len({moment.astimezone(UTC) for moment in instants})


def test_seconds_between_two_timestamps():
    # Speed-to-lead is the difference between "received" and "first contacted".
    assert seconds_between("2026-11-11T22:31:05.123Z", "2026-11-11T22:31:06.623Z") == 1.5
    assert seconds_between("2026-11-11T22:31:05.000Z", "2026-11-11T22:31:05.000Z") == 0
    assert seconds_between("2026-11-11T00:00:00.000Z", "2026-11-12T00:00:00.000Z") == 86_400
    assert seconds_between("2026-11-11T22:31:06.000Z", "2026-11-11T22:31:05.000Z") == -1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, None),
        (None, "2026-11-11T22:31:05.000Z"),
        ("2026-11-11T22:31:05.000Z", None),
        ("", "2026-11-11T22:31:05.000Z"),
        ("2026-11-11T22:31:05.000Z", ""),
    ],
)
def test_seconds_between_is_none_when_either_end_is_missing(start, end):
    # A lead that was never contacted has no response time, not a zero one.
    assert seconds_between(start, end) is None


# --- money ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (None, "—"),
        (0, "$0.00"),
        (1, "$0.01"),
        (5, "$0.05"),
        (10, "$0.10"),
        (99, "$0.99"),
        (100, "$1.00"),
        (2567, "$25.67"),
        (2800, "$28.00"),
        (123450, "$1,234.50"),
        (100000000, "$1,000,000.00"),
    ],
)
def test_format_cents(cents, expected):
    # Fees are shown in dollars with two decimals; an unknown fee is a dash.
    assert format_cents(cents) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (25.67, 2567),
        ("25.67", 2567),
        ("$25.67", 2567),
        ("$1,234.50", 123450),
        ("1,234.50", 123450),
        (" 12 ", 1200),
        (0, 0),
        ("0", 0),
        (0.0, 0),
        (1, 100),
        (28, 2800),
        (0.1 + 0.2, 30),
        (19.99, 1999),
        (29.99, 2999),
        (0.29, 29),
        (0.57, 57),
        (1.15, 115),
        (4.35, 435),
        (33.33, 3333),
        (1e3, 100000),
        ("1e3", 100000),
        (0.01, 1),
        ("0.01", 1),
        ("12.", 1200),
        (".5", 50),
    ],
)
def test_to_cents_converts_dollar_amounts_exactly(raw, expected):
    # Money is stored as integer cents; float artefacts must never leak into a credit claim.
    assert to_cents(raw) == expected


def test_to_cents_is_exact_for_every_two_decimal_float_up_to_1000_dollars():
    # Angi sends the fee as a JSON float; no value may come out a cent off.
    for cents in range(0, 100_000):
        assert to_cents(cents / 100) == cents
        assert to_cents(f"{cents / 100:.2f}") == cents


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.005", 1), ("0.004", 0), ("1.005", 101), ("2.675", 268), ("2.674", 267), (2.675, 268), ("0.999", 100)],
)
def test_to_cents_rounds_half_up_to_the_nearest_cent(raw, expected):
    # A third decimal is rounded the way an invoice would be, not the way binary floats are.
    assert to_cents(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "abc",
        "$",
        "12 USD",
        "twenty",
        "12.34.56",
        True,
        False,
        [],
        [25.67],
        {},
        {"amount": 25.67},
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "Infinity",
        "-Infinity",
        "inf",
    ],
)
def test_to_cents_of_something_that_is_not_an_amount_is_none(raw):
    # Junk in the fee field means "fee unknown"; it must not become 0 or 1 or crash.
    assert to_cents(raw) is None


@pytest.mark.parametrize("raw", [-5, -0.01, "-5", "-25.67", "$-5"])
def test_to_cents_rejects_negative_amounts(raw):
    # A lead never has a negative price.
    assert to_cents(raw) is None


def test_to_cents_always_returns_an_int_or_none():
    # The database column is an INTEGER.
    for raw in (25.67, "25.67", 0, 1e3, 0.1 + 0.2):
        assert type(to_cents(raw)) is int


# --- flags -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        (" False ", False),
        (" true\n", True),
        ("yes", True),
        ("no", False),
        ("Y", True),
        ("n", False),
        ("1", True),
        ("0", False),
        (1, True),
        (0, False),
    ],
)
def test_to_bool_understands_the_spellings_angi_uses(raw, expected):
    # Angi sends flags as true, "true" and "false"; the string "false" must be False.
    assert to_bool(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "  ", "maybe", "unknown", "null", "2", 2, -1, 0.5, [], [True], {}, {"value": True}])
def test_to_bool_of_anything_else_is_none(raw):
    # "Angi did not say" is different from "no", so unknown values are None, never False.
    assert to_bool(raw) is None


def test_to_bool_never_uses_truthiness():
    # Non-empty strings are truthy in Python; that must not turn "false"/"no"/"0" into True.
    for text in ("false", "no", "0", "n", "False", "NO"):
        assert bool(text) is True
        assert to_bool(text) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "boolean"),
        (False, "boolean"),
        (0, "number"),
        (1, "number"),
        (-7, "number"),
        (25.67, "number"),
        (10**30, "number"),
        ("", "string"),
        ("text", "string"),
        ("5", "string"),
        ("true", "string"),
        ([], "array"),
        ([1, 2], "array"),
        ({}, "object"),
        ({"a": 1}, "object"),
    ],
)
def test_json_type_names_every_json_type(value, expected):
    # Drift reports name types the way JSON does; a boolean is not a number (even though bool is an int in Python).
    assert json_type(value) == expected


def test_json_type_agrees_with_the_json_decoder():
    # Whatever json.loads produces is classified by its JSON type.
    decoded = json.loads('{"n": null, "b": true, "i": 1, "f": 1.5, "s": "x", "a": [], "o": {}}')
    assert {key: json_type(value) for key, value in decoded.items()} == {
        "n": "null",
        "b": "boolean",
        "i": "number",
        "f": "number",
        "s": "string",
        "a": "array",
        "o": "object",
    }


# --- ids and json ------------------------------------------------------------------------------


def test_new_ids_carry_their_prefix_and_do_not_repeat():
    # Ids are self-describing ("lead_...") and unique.
    ids = {new_id("lead") for _ in range(500)}
    assert len(ids) == 500
    assert all(value.startswith("lead_") and len(value) == len("lead_") + 16 for value in ids)


def test_new_tokens_are_url_safe_and_do_not_repeat():
    # Booking links must be unguessable and need no URL escaping.
    tokens = {new_token() for _ in range(500)}
    assert len(tokens) == 500
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert all(len(token) >= 24 and set(token) <= allowed for token in tokens)


def test_dumps_is_compact_and_independent_of_key_order():
    # Stored JSON (and the drift signature built from it) must not depend on dict ordering.
    assert dumps({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert dumps({"b": 1, "a": 2}) == dumps({"a": 2, "b": 1})
    assert dumps({"when": datetime(2026, 11, 12, 15, 0, tzinfo=UTC)}) == '{"when":"2026-11-12 15:00:00+00:00"}'


@pytest.mark.parametrize("raw", [None, "", "not json", "{", "[1,"])
def test_loads_falls_back_to_the_default(raw):
    # A missing or damaged JSON column reads as "nothing", not as a crash.
    assert loads(raw) is None
    assert loads(raw, {}) == {}


def test_loads_reads_back_what_dumps_wrote():
    # Round trip for the JSON columns.
    value = {"issues": ["a", "b"], "drift": {"unknown": [], "n": 1}, "text": "– é 😀"}
    assert loads(dumps(value)) == value
