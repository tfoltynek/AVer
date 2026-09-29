from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import BadRequest
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from core import authorship
from core.forms import TestSuccessEstimateForm
from core.models import Document, Test, TestedParagraph, Word
from core.tasks import dispatch_score_paragraph, dispatch_score_test
from core.views.selectors import (
    document_for_test,
    documents_for_test_picker,
    pick_test_words,
)


class TestListView(LoginRequiredMixin, View):
    def get(self, request):
        object_list = Test.objects.filter(user=self.request.user).order_by(
            "-created_at"
        )
        if request.headers.get("HX-Request"):
            return render(
                request,
                "core/partials/test_list.html",
                {"object_list": object_list},
            )
        else:
            return render(request, "core/tests.html", {"object_list": object_list})


class TestSelectView(LoginRequiredMixin, View):
    def get(self, request):
        context = {"picker_documents": documents_for_test_picker(request.user)}
        return render(request, "core/test_select.html", context)


class TestCreateView(LoginRequiredMixin, View):
    def get(self, request, type):
        if type not in ["previewML", "randomML"]:
            raise BadRequest("Nope")

        document_id = request.GET.get("document", "")
        if document_id:
            picker = documents_for_test_picker(request.user)
            document = (
                picker.filter(pk=document_id).first()
                if document_id.isdecimal() and len(document_id) < 19
                else None
            )
            if document is None:
                messages.error(
                    request,
                    _("That document is not available for test generation."),
                )
                return redirect(reverse("test-select"))
        else:
            try:
                document = document_for_test(request.user)
            except BadRequest:
                messages.error(
                    request,
                    _(
                        "You have no analyzed document yet. Upload a document and "
                        "wait for its analysis to finish."
                    ),
                )
                return redirect(reverse("test-select"))

        test_words = pick_test_words(document=document, user=request.user)
        if not test_words:
            messages.info(
                request,
                _("There are no new words available for a test on this document."),
            )
            return redirect(reverse("test-select"))

        with transaction.atomic():
            new_test = Test.objects.create(
                document=document,
                user=request.user,
                type=type,
                show_preview=type == "previewML",
            )
            TestedParagraph.objects.bulk_create(
                [TestedParagraph(test=new_test, word=word) for word in test_words]
            )

        return redirect(reverse("document-test", args=[new_test.pk]))


def create_try_document_test(request, document_id):
    document = get_object_or_404(Document, pk=document_id, uploaded_by__isnull=True)
    new_test = Test.objects.create(document=document, type="try")

    test_words = Word.objects.filter(paragraph__document=document)
    test_paragraphs = [TestedParagraph(test=new_test, word=word) for word in test_words]
    TestedParagraph.objects.bulk_create(test_paragraphs)

    return redirect(reverse("document-test", args=[new_test.pk]))


class CreateDocumentTestView(LoginRequiredMixin, View):
    def get(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id, uploaded_by=request.user)

        if document.analysis_state != "completed":
            messages.info(
                request,
                _(
                    "This document is still being analyzed. "
                    "Please try again in a moment."
                ),
            )
            return redirect(reverse("dashboard"))

        test_words = pick_test_words(document=document, user=request.user)
        if not test_words:
            messages.info(
                request,
                _("There are no new words available for a test on this document."),
            )
            return redirect(reverse("dashboard"))

        with transaction.atomic():
            new_test = Test.objects.create(
                document=document, user=request.user, type="authorML"
            )
            TestedParagraph.objects.bulk_create(
                [TestedParagraph(test=new_test, word=word) for word in test_words]
            )

        return redirect(reverse("document-test", args=[new_test.pk]))


class TestView(View):
    def _get_test(self, request, test_id):
        # Anonymous can access only "try" tests (user IS NULL).
        # Authenticated users can access try tests OR their own.
        qs = Test.objects.filter(pk=test_id)
        if request.user.is_authenticated:
            qs = qs.filter(Q(user__isnull=True) | Q(user=request.user))
        else:
            qs = qs.filter(user__isnull=True)
        return get_object_or_404(qs)

    def _get_test_paragraphs(self, test):
        return TestedParagraph.objects.select_related("word").filter(test=test)

    def _get_next_unanswered_paragraph(self, test):
        return (
            self._get_test_paragraphs(test).filter(answer_ended_at__isnull=True).first()
        )

    def _render_next_paragraph_or_redirect(
        self,
        request,
        test: Test,
    ):
        next_paragraph = self._get_next_unanswered_paragraph(test)

        if next_paragraph:
            next_paragraph.answer_started_at = timezone.now()
            next_paragraph.save()
            return render(
                request,
                "core/partials/document_test.html",
                {"paragraph": next_paragraph, "test": test},
            )
        else:
            response = HttpResponse()
            response["HX-Redirect"] = reverse(
                "document-test",
                args=[test.pk],
            )

            return response


class DocumentTestView(TestView):
    def get(self, request, *args, **kwargs):
        test = self._get_test(request, kwargs["test_id"])
        if test.show_preview:
            paragraphs = self._get_test_paragraphs(test)
            return render(
                request,
                "core/document_test_preview.html",
                {"paragraph_list": paragraphs, "test": test},
            )
        next_paragraph = self._get_next_unanswered_paragraph(test)
        if next_paragraph:
            next_paragraph.answer_started_at = timezone.now()
            next_paragraph.save()
            return render(
                request,
                f"core/{"try_" if not request.user.is_authenticated else ""}document_test.html",
                {
                    "paragraph": next_paragraph,
                    "test": test,
                },
            )
        elif test.success_estimate is None:
            form = TestSuccessEstimateForm()
            return render(
                request,
                f"core/{"try_" if not request.user.is_authenticated else ""}document_test_success_estimate.html",
                {"form": form, "test": test},
            )
        else:
            prefix = "try_" if not request.user.is_authenticated else ""
            if not test.scoring_complete:
                # The polling partial sends X-Score-Poll on every poll. While
                # scoring is still running, respond with 204 so htmx leaves the
                # spinner alone. Once it's done, return HX-Refresh so htmx
                # navigates the whole page to the real score view.
                if request.headers.get("X-Score-Poll"):
                    response = HttpResponse(status=204)
                    return response
                # Self-heal: submitted tests with no computed_score on every
                # paragraph (pre-0017 tests, or ones whose celery task was
                # lost) would otherwise spin forever. score_test is idempotent.
                dispatch_score_test(str(test.pk))
                return render(
                    request,
                    f"core/{prefix}document_test_scoring.html",
                    {"test": test},
                )

            if request.headers.get("X-Score-Poll"):
                # Scoring just finished — trigger a real page navigation.
                response = HttpResponse(status=204)
                response["HX-Refresh"] = "true"
                return response

            paragraphs = self._get_test_paragraphs(test)
            report = authorship.report(paragraphs)

            duration_human = ""
            if test.submitted_at and test.created_at:
                duration_seconds = int(
                    (test.submitted_at - test.created_at).total_seconds()
                )
                minutes, seconds = divmod(max(duration_seconds, 0), 60)
                if minutes:
                    duration_human = f"{minutes} min {seconds} s"
                else:
                    duration_human = f"{seconds} s"
            return render(
                request,
                f"core/{prefix}document_test_score.html",
                {
                    "paragraph_list": paragraphs,
                    "test": test,
                    "report": report,
                    "test_duration_human": duration_human,
                    "test_total_items": len(paragraphs),
                },
            )

    def post(self, request, *args, **kwargs):
        test = self._get_test(request, kwargs["test_id"])
        next_paragraph = self._get_next_unanswered_paragraph(test)
        just_answered_pk = None

        if next_paragraph:
            self._save_answered_words(request, next_paragraph)
            next_paragraph.answer_ended_at = timezone.now()
            next_paragraph.save()
            just_answered_pk = next_paragraph.pk

        # Build the response (which also flips the *next* paragraph's
        # answer_started_at) before firing the score task. Otherwise, in
        # CELERY_TASK_ALWAYS_EAGER mode the task runs inline between the two
        # saves and any concurrent DB reader (e.g. e2e tests) can see a state
        # where the just-answered paragraph is ended but the next one hasn't
        # started yet, which breaks the "find current paragraph" query.
        response = self._render_next_paragraph_or_redirect(request, test)

        if just_answered_pk is not None:
            dispatch_score_paragraph(just_answered_pk)

        return response

    def _save_answered_words(self, request, paragraph):
        answered_words = [
            value.strip()
            for key, value in request.POST.items()
            if key.startswith("word_")
        ]
        paragraph.answered_word = " ".join(answered_words) if answered_words else None


class DocumentTestSkipView(TestView):
    def post(self, request, *args, **kwargs):
        test = self._get_test(request, kwargs["test_id"])
        self._skip_current_paragraph(test)

        return self._render_next_paragraph_or_redirect(request, test)

    def _skip_current_paragraph(self, test):
        next_paragraph = self._get_next_unanswered_paragraph(test)
        if next_paragraph:
            next_paragraph.answer_ended_at = timezone.now()
            next_paragraph.save()

        return next_paragraph


class DocumentTestSuccessEstimateView(TestView):
    def post(self, request, *args, **kwargs):
        test = self._get_test(request, kwargs["test_id"])
        form = TestSuccessEstimateForm(request.POST)
        if form.is_valid():
            test.success_estimate = form.cleaned_data["success_estimate"]
            test.submitted_at = timezone.now()
            test.save()
            dispatch_score_test(str(test.pk))
            return redirect(reverse("document-test", args=[test.pk]))

        return render(
            request,
            f"core/{"try_" if not request.user.is_authenticated else ""}document_test_success_estimate.html",
            {"form": form, "test": test},
        )

class DocumentTestPreviewView(TestView):
    def post(self, request, *args, **kwargs):
        test = self._get_test(request, kwargs["test_id"])
        test.show_preview = False
        test.save()
        response = HttpResponse()
        response["HX-Redirect"] = reverse(
                "document-test",
                args=[test.pk],
            )

        return response
