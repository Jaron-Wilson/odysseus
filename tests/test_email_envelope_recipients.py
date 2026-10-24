"""Regression: SMTP envelope recipients must be parsed, not split on bare commas.

The send paths built the RCPT TO list with `field.split(",")`, which corrupts a
display name containing a comma (e.g. `"Smith, John" <john@corp.com>`, the common
Outlook / corporate address-book form): it splits into `"Smith` and
`John" <john@corp.com>`, so the broken fragments are handed to smtp.sendmail and
delivery fails. `_envelope_recipients` uses email.utils.getaddresses instead.
"""
import routes.email_routes as email_routes


def test_display_name_with_comma_yields_one_address():
    assert email_routes._envelope_recipients('"Smith, John" <john@corp.com>') == ["john@corp.com"]


def test_multiple_plain_addresses():
    assert email_routes._envelope_recipients("a@x.com, b@y.com") == ["a@x.com", "b@y.com"]


def test_to_cc_bcc_combined_and_none_safe():
    got = email_routes._envelope_recipients('"Doe, Jane" <jane@x.com>, bob@y.com', None, "carol@z.com")
    assert got == ["jane@x.com", "bob@y.com", "carol@z.com"]


def test_empty_and_none_fields():
    assert email_routes._envelope_recipients("", None) == []


def test_trailing_comma_keeps_the_address():
    # Compose chips leave "a@b.com," behind; strict getaddresses returned [('', '')] for the whole field, the
    # envelope came out empty and smtplib failed with a bare "{}".
    assert email_routes._envelope_recipients("jaron@jarondynamics.com,") == ["jaron@jarondynamics.com"]
    assert email_routes._envelope_recipients("a@x.com, b@y.com,") == ["a@x.com", "b@y.com"]


def test_leading_and_empty_slots():
    assert email_routes._envelope_recipients(", a@x.com, , b@y.com") == ["a@x.com", "b@y.com"]


def test_clean_address_field_keeps_display_names():
    assert email_routes._clean_address_field('"Smith, John" <john@corp.com>, ') == '"Smith, John" <john@corp.com>'
    assert email_routes._clean_address_field(None) == ""
