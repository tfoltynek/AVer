"""Test generation from a chosen document.

Admins may generate a Reviewer/Tester test from any document with a
completed AI analysis via ?document=<id> on the test-create URL. Everyone
else is confined to their own uploads: the same parameter over someone
else's document is refused, and the picker only ever lists what the
requester may pick.
"""

import datetime

import pytest
from django.urls import reverse

from core.models import AnalysisJob, Document, Language, Paragraph, Test, Word
from core.views import selectors


@pytest.fixture
def admin_user(user_factory):
    user = user_factory(email="admin@example.com")
    user.is_admin = True
    user.save()
    return user


def _make_analyzed_document(
    *, title, uploader, language_code="cs", analyzed=True, with_words=True
):
    """A document owned by ``uploader``. For a requester who is not the
    uploader, only the admin override can ever reach it."""
    language = Language.objects.get(code=language_code)
    document = Document.objects.create(
        file=f"documents/{title}.txt",
        title=title,
        language=language,
        publication_date=datetime.date(2026, 1, 1),
        uploaded_by=uploader,
    )
    if analyzed:
        AnalysisJob.objects.create(
            document=document, analysis_type="ai", status="completed"
        )
    if with_words:
        for shape, content in [
            (Word.Shape.UNIGRAM, "day"),
            (Word.Shape.BIGRAM, "cold day"),
            (Word.Shape.TRIGRAM, "bright cold day"),
        ]:
            paragraph = Paragraph.objects.create(
                document=document,
                content=f"It was a bright cold day in April ({shape}).",
                language=language,
            )
            Word.objects.create(
                paragraph=paragraph,
                content=content,
                sentence_blanked="It was a <<BLANK>> in April.",
                source=Word.Source.MUNI_API,
                shape=shape,
            )
    return document


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_analyzed_documents_requires_completed_ai_job(user_factory):
    uploader = user_factory(email="uploader@example.com")
    analyzed = _make_analyzed_document(title="analyzed", uploader=uploader)
    unanalyzed = _make_analyzed_document(
        title="unanalyzed", uploader=uploader, analyzed=False
    )
    failed = _make_analyzed_document(title="failed", uploader=uploader, analyzed=False)
    AnalysisJob.objects.create(document=failed, analysis_type="ai", status="failed")
    basic_only = _make_analyzed_document(
        title="basic-only", uploader=uploader, analyzed=False
    )
    AnalysisJob.objects.create(
        document=basic_only, analysis_type="basic", status="completed"
    )

    documents = list(selectors.analyzed_documents())

    assert analyzed in documents
    assert unanalyzed not in documents
    assert failed not in documents
    assert basic_only not in documents


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_analyzed_documents_ordered_by_title(user_factory):
    uploader = user_factory(email="uploader@example.com")
    _make_analyzed_document(title="zebra", uploader=uploader)
    _make_analyzed_document(title="alpha", uploader=uploader)

    titles = [document.title for document in selectors.analyzed_documents()]

    assert titles == sorted(titles)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_admin_generates_test_from_chosen_document(client, admin_user, user_factory):
    uploader = user_factory(email="uploader@example.com")
    document = _make_analyzed_document(title="chosen", uploader=uploader)
    client.force_login(admin_user)

    response = client.get(
        reverse("test-create", args=["randomML"]), {"document": document.pk}
    )

    test = Test.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("document-test", args=[test.pk])
    assert test.document == document
    assert test.user == admin_user
    assert test.type == "randomML"
    assert test.show_preview is False
    assert test.paragraphs.count() == 3


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_admin_reviewer_variant_keeps_preview(client, admin_user, user_factory):
    uploader = user_factory(email="uploader@example.com")
    document = _make_analyzed_document(title="chosen", uploader=uploader)
    client.force_login(admin_user)

    client.get(reverse("test-create", args=["previewML"]), {"document": document.pk})

    test = Test.objects.get()
    assert test.type == "previewML"
    assert test.show_preview is True


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_non_admin_cannot_pick_someone_elses_document(client, user_factory):
    uploader = user_factory(email="uploader@example.com")
    taker = user_factory(email="taker@example.com")
    document = _make_analyzed_document(title="chosen", uploader=uploader)
    client.force_login(taker)

    response = client.get(
        reverse("test-create", args=["randomML"]),
        {"document": document.pk},
        follow=True,
    )

    assert response.redirect_chain[-1][0] == reverse("test-select")
    assert Test.objects.count() == 0
    rendered_messages = [str(m) for m in response.context["messages"]]
    assert any("not available" in m for m in rendered_messages)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_non_admin_generates_from_own_document(client, user_factory):
    owner = user_factory(email="owner@example.com")
    document = _make_analyzed_document(title="mine", uploader=owner)
    client.force_login(owner)

    response = client.get(
        reverse("test-create", args=["randomML"]), {"document": document.pk}
    )

    test = Test.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("document-test", args=[test.pk])
    assert test.document == document
    assert test.user == owner
    assert test.paragraphs.count() == 3


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.parametrize(
    "bad_id", ["999999", "abc", "³", "9999999999999999999999999"]
)
def test_admin_invalid_document_id_bounces(client, admin_user, bad_id):
    client.force_login(admin_user)

    response = client.get(
        reverse("test-create", args=["randomML"]), {"document": bad_id}, follow=True
    )

    assert response.redirect_chain[-1][0] == reverse("test-select")
    assert Test.objects.count() == 0
    rendered_messages = [str(m) for m in response.context["messages"]]
    assert any("not available" in m for m in rendered_messages)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_admin_unanalyzed_document_bounces(client, admin_user, user_factory):
    uploader = user_factory(email="uploader@example.com")
    document = _make_analyzed_document(
        title="unanalyzed", uploader=uploader, analyzed=False
    )
    client.force_login(admin_user)

    response = client.get(
        reverse("test-create", args=["randomML"]), {"document": document.pk}
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("test-select")
    assert Test.objects.count() == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_admin_wordless_document_bounces_without_empty_test(
    client, admin_user, user_factory
):
    uploader = user_factory(email="uploader@example.com")
    document = _make_analyzed_document(
        title="wordless", uploader=uploader, with_words=False
    )
    client.force_login(admin_user)

    response = client.get(
        reverse("test-create", args=["randomML"]), {"document": document.pk}
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("test-select")
    assert Test.objects.count() == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_picker_lists_every_document_for_admins(client, admin_user, user_factory):
    uploader = user_factory(email="uploader@example.com")
    _make_analyzed_document(title="somebody-elses", uploader=uploader)

    client.force_login(admin_user)
    admin_html = client.get(reverse("test-select")).content.decode()

    assert 'name="document"' in admin_html
    assert "somebody-elses" in admin_html


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_picker_lists_own_documents_only_for_regular_users(client, user_factory):
    uploader = user_factory(email="uploader@example.com")
    taker = user_factory(email="taker@example.com")
    _make_analyzed_document(title="somebody-elses", uploader=uploader)
    _make_analyzed_document(title="mine", uploader=taker)

    client.force_login(taker)
    taker_html = client.get(reverse("test-select")).content.decode()

    assert 'name="document"' in taker_html
    assert "mine" in taker_html
    assert "somebody-elses" not in taker_html


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_variant_buttons_submit_to_test_create(client, user_factory):
    taker = user_factory(email="taker@example.com")
    client.force_login(taker)

    html = client.get(reverse("test-select")).content.decode()

    assert f'formaction="{reverse("test-create", args=["previewML"])}"' in html
    assert f'formaction="{reverse("test-create", args=["randomML"])}"' in html


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_admin_empty_document_param_uses_automatic_flow(client, admin_user):
    # The admin uploaded nothing, so the automatic flow bounces to
    # test-select; an honored empty id would 500 or 404 instead.
    client.force_login(admin_user)

    response = client.get(reverse("test-create", args=["randomML"]), {"document": ""})

    assert response.status_code == 302
    assert response["Location"] == reverse("test-select")
    assert Test.objects.count() == 0
