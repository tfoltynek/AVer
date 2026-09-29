"""Sign-up enforces the password rules, not only the help text.

`SignUpForm` used to check only that the two password fields match, so the
ten-character minimum and the other AUTH_PASSWORD_VALIDATORS held at password
change but not at registration.
"""

import pytest
from django.urls import reverse
from django.utils import translation

from core.forms import SignUpForm
from core.models import AcademicField, User

# Six letters and a digit: trips only the length rule (not numeric, not in Django's common-password list).
SHORT = "zvqx7fm"
TOO_SHORT_EN = "The password must have at least 10 characters."
TOO_SHORT_CS = "Heslo musí mít alespoň 10 znaků."


def _signup_data(password, email="candidate@example.com"):
    return {
        "email": email,
        "gender": "M",
        "education": "bachelors",
        "academic_fields": [AcademicField.objects.first().pk],
        "password": password,
        "password_confirm": password,
    }


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_rejects_a_short_password():
    form = SignUpForm(_signup_data(SHORT))

    assert not form.is_valid()
    assert form.errors["password"] == [TOO_SHORT_EN]
    assert not User.objects.filter(email="candidate@example.com").exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_rejects_an_all_numeric_password():
    form = SignUpForm(_signup_data("1234567890"))

    assert not form.is_valid()
    assert "This password is entirely numeric." in form.errors["password"]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_rejects_a_password_similar_to_the_email():
    form = SignUpForm(_signup_data("jana.novakova", email="jana.novakova@example.com"))

    assert not form.is_valid()
    assert any("too similar" in message for message in form.errors["password"])


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_accepts_a_ten_character_password():
    form = SignUpForm(_signup_data("kv9xq2mz7p"))

    assert form.is_valid(), form.errors


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_short_password_message_is_czech():
    with translation.override("cs"):
        form = SignUpForm(_signup_data(SHORT))
        assert not form.is_valid()
        assert form.errors["password"] == [TOO_SHORT_CS]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_signup_partial_shows_the_short_password_error(client):
    response = client.post(reverse("sign-up"), _signup_data(SHORT), HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    content = response.content.decode()
    assert TOO_SHORT_EN in content
    assert "<html" not in content
    assert not User.objects.filter(email="candidate@example.com").exists()
