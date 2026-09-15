"""How a contact address maps to a Memory Store identifier.

Getting this wrong looks like a returning family having no memory at all rather
than like an error, which is exactly how it presented: `tac.retrieve_memory`
derives the type as email-or-phone, so it looked up a WhatsApp address under
`phone` with the prefix attached, matched nothing, and reported no memory while
observations accumulated in the store regardless.
"""

import pytest

from app.memory import identifier_for


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("whatsapp:+13035550100", ("whatsapp", "whatsapp:+13035550100")),
        ("+13035550100", ("phone", "+13035550100")),
        ("sms:+13035550100", ("phone", "+13035550100")),
        ("chat:user-1", ("chat", "chat:user-1")),
        ("someone@example.org", ("email", "someone@example.org")),
    ],
)
def test_the_identifier_a_profile_is_keyed_on(address, expected):
    """A WhatsApp profile is keyed on the whole address including the prefix,
    an SMS one on the bare E.164 number. Looking up the wrong type returns no
    profile, which is indistinguishable from a first-time contact."""
    assert identifier_for(address) == expected


def test_the_same_person_on_both_channels_has_two_identifiers():
    """`phone` and `whatsapp` are separate identifier types, so one number on
    both channels resolves to two identifiers rather than one."""
    whatsapp = identifier_for("whatsapp:+13035550100")
    sms = identifier_for("+13035550100")

    assert whatsapp != sms
    assert whatsapp[0] != sms[0]
