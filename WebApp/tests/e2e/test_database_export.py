"""The data export must not assume every test has a taker.

Try-mode tests are created with ``user=None``. Both export formats read the
participant fields (id, gender, education, proficiencies) straight off
``test.user``, so a single anonymous row used to take the whole export down
with an AttributeError — for every user, in both formats.
"""

import csv
import datetime
import io
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import Document, Language, Paragraph, Test, TestedParagraph, Word


def _submitted_test(*, user, title, success_estimate=None, index=4):
    """A submitted, answered test — the shape the export actually collects."""
    document = Document.objects.create(
        file=f"documents/{title}.txt",
        title=title,
        language=Language.objects.filter(code="en").first(),
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=user,
    )
    paragraph = Paragraph.objects.create(
        document=document, content="A sentence with a gap in it.", language=document.language
    )
    word = Word.objects.create(
        paragraph=paragraph,
        content="gap",
        sentence_blanked="A sentence with a <<BLANK>> in it.",
        sentence_index=0,
        index=index,
        source=Word.Source.MUNI_API,
        shape=Word.Shape.UNIGRAM,
        selection_method="ml_noun_unigram",
    )
    test = Test.objects.create(
        document=document,
        user=user,
        type="authorML" if user else "try",
        submitted_at=timezone.now(),
        success_estimate=success_estimate,
    )
    TestedParagraph.objects.create(
        test=test,
        word=word,
        answered_word="gap",
        answer_started_at=timezone.now(),
        answer_ended_at=timezone.now(),
        computed_score=1.0,
        computed_grade="correct",
    )
    return test


@pytest.fixture
def exporter(user_factory):
    user = user_factory(email="exporter@example.com")
    user.can_export_data = True
    user.save()
    return user


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_json_export_survives_a_test_with_no_user(client, exporter):
    mine = _submitted_test(user=exporter, title="mine")
    _submitted_test(user=None, title="anonymous")
    client.force_login(exporter)

    response = client.get(reverse("database-export-default"))

    assert response.status_code == 200
    payload = json.loads(response.content)
    exported = {entry["test"]["id"] for entry in payload["tests"]}
    assert exported == {str(mine.id)}, "only tests with a taker belong in the export"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_csv_export_survives_a_test_with_no_user(client, exporter):
    mine = _submitted_test(user=exporter, title="mine")
    _submitted_test(user=None, title="anonymous")
    client.force_login(exporter)

    response = client.get(reverse("database-export", args=["csv"]))

    assert response.status_code == 200
    body = b"".join(response.streaming_content) if response.streaming else response.content
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    assert [row["test_id"] for row in rows] == [str(mine.id)]
    assert rows[0]["success_estimate"] == "", "a NULL estimate stays an empty cell"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_csv_export_keeps_zero_values(client, exporter):
    """0 is data: an estimate of 0 % and a blank in the first sentence."""
    test = _submitted_test(user=exporter, title="zeros", success_estimate=0, index=0)
    client.force_login(exporter)

    response = client.get(reverse("database-export", args=["csv"]))

    body = b"".join(response.streaming_content) if response.streaming else response.content
    row = next(csv.DictReader(io.StringIO(body.decode())))
    assert row["test_id"] == str(test.id)
    assert row["success_estimate"] == "0"
    assert row["answer_1_sentence_index"] == "0"
    assert row["answer_1_index"] == "0"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_json_export_keeps_zero_values(client, exporter):
    test = _submitted_test(user=exporter, title="zeros", success_estimate=0, index=0)
    client.force_login(exporter)

    response = client.get(reverse("database-export-default"))

    payload = json.loads(response.content)
    entry = next(e for e in payload["tests"] if e["test"]["id"] == str(test.id))
    assert entry["test"]["success_estimate"] == 0
