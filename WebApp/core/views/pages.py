import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import connection
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.generic import View

from core.forms import LanguageProficiencyForm
from core.models import (
    Document,
    LanguageProficiency,
    Test,
    TestedParagraph,
)
from core.muni import get_muni_client
from core.views.selectors import eligible_documents

logger = logging.getLogger(__name__)

EXTERNAL_SERVICES = [
    {
        "key": "muni_nlp",
        "name": "MUNI NLP (AI analysis)",
    },
]


class HealthCheckView(View):
    """Liveness + readiness probe for load balancers and uptime monitors.

    - GET /healthz       → cheap liveness check (process up, no DB call)
    - GET /healthz?deep  → also opens a DB cursor; 503 if the DB is down

    Both responses are JSON. Intentionally public so external monitors do
    not need credentials.
    """

    def get(self, request):
        deep = "deep" in request.GET
        if not deep:
            return JsonResponse({"status": "ok", "checks": {"process": "ok"}})

        checks = {"process": "ok"}
        status = 200
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"error: {exc.__class__.__name__}"
            status = 503

        return JsonResponse(
            {"status": "ok" if status == 200 else "degraded", "checks": checks},
            status=status,
        )


class LandingPageView(View):
    def get(self, request):
        return render(request, "core/index.html")


class DataPageView(View):
    def get(self, request):
        return render(request, "core/data.html")


class DashboardView(LoginRequiredMixin, View):
    """The three-step guide: upload a document, generate a test from it,
    fill the test in. Every step is about the user's own documents."""

    def get(self, request, *args, **kwargs):
        try:
            document = Document.objects.filter(uploaded_by=request.user).latest(
                "uploaded_at"
            )
        except Document.DoesNotExist:
            document = None

        # Step 2 unlocks on the same queryset that fills the test-select
        # picker, so the guide and that page cannot disagree about whether
        # a test can be generated.
        has_analyzed_document = eligible_documents(request.user).exists()

        own_tests = Test.objects.filter(
            user=request.user, document__uploaded_by=request.user
        )
        generated_test_count = own_tests.count()
        submitted_test_count = own_tests.filter(submitted_at__isnull=False).count()
        pending_test = (
            own_tests.filter(submitted_at__isnull=True).order_by("-created_at").first()
        )

        steps_done = sum(
            [
                1 if document else 0,
                1 if generated_test_count else 0,
                1 if submitted_test_count else 0,
            ]
        )

        return render(
            request,
            "core/dashboard.html",
            {
                "document": document,
                "has_analyzed_document": has_analyzed_document,
                "generated_test_count": generated_test_count,
                "submitted_test_count": submitted_test_count,
                "pending_test": pending_test,
                "steps_done": steps_done,
                "steps_total": 3,
            },
        )


class SettingsView(LoginRequiredMixin, View):
    def get(self, request):
        language_list = LanguageProficiency.objects.filter(user=self.request.user)
        language_form = LanguageProficiencyForm(user=request.user)
        user = self.request.user
        return render(
            request,
            "core/settings.html",
            {
                "user": user,
                "language_form": language_form,
                "language_list": language_list,
            },
        )


class ServiceStatusView(LoginRequiredMixin, View):
    """Admin-only liveness check for configured external services."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_admin:
            return render(
                request,
                "core/partials/service-status-list.html",
                {
                    "results": [],
                    "fatal_error": _("Forbidden — admin only."),
                },
                status=403,
            )
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, key=None):
        services = [s for s in EXTERNAL_SERVICES if key is None or s["key"] == key]
        results = [self._probe(s) for s in services]
        return render(
            request,
            "core/partials/service-status-list.html",
            {"results": results},
        )

    def _probe(self, service):
        client = get_muni_client()
        result = client.probe()
        return {
            "key": service["key"],
            "name": service["name"],
            "url": client.submit_url,
            "is_up": result.is_up,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }


def _cell(value):
    """A CSV cell: empty for NULL, otherwise the value itself, so 0 stays 0."""
    return "" if value is None else value


class DatabaseExportView(LoginRequiredMixin, View):
    """
    View for exporting database data. Only accessible by admin users.
    Supports JSON and CSV export formats.
    """

    def dispatch(self, request, *args, **kwargs):
        # Anonymous users go through LoginRequiredMixin's redirect path; only
        # check the custom flag once we know we have a real User instance
        # (AnonymousUser has no `can_export_data`).
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.can_export_data:
            from django.http import HttpResponseForbidden

            return HttpResponseForbidden(
                _("You don't have permission to access this resource.")
            )
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, format_type="json"):
        from datetime import datetime

        # Parse date parameters from request
        from_date = request.GET.get("from_date")
        to_date = request.GET.get("to_date")

        # Convert string dates to date objects if provided
        if from_date:
            try:
                from_date = datetime.strptime(from_date, "%Y-%m-%d").date()
            except ValueError:
                from django.http import HttpResponseBadRequest

                return HttpResponseBadRequest(
                    _("Invalid from_date format. Use YYYY-MM-DD.")
                )

        if to_date:
            try:
                to_date = datetime.strptime(to_date, "%Y-%m-%d").date()
            except ValueError:
                from django.http import HttpResponseBadRequest

                return HttpResponseBadRequest(_("Invalid to_date format. Use YYYY-MM-DD."))

        export_date = datetime.now().strftime("%Y-%m-%d")
        export_file_name = f"aver-export_{export_date}"

        # Add date range to filename if specified
        if from_date or to_date:
            date_suffix = ""
            if from_date and to_date:
                date_suffix = f"_{from_date}_to_{to_date}"
            elif from_date:
                date_suffix = f"_from_{from_date}"
            elif to_date:
                date_suffix = f"_to_{to_date}"
            export_file_name = f"aver-export{date_suffix}_{export_date}"

        try:
            if format_type == "json":
                return self._export_json(export_file_name, from_date, to_date)
            elif format_type == "csv":
                return self._export_csv(export_file_name, from_date, to_date)
            else:
                from django.http import HttpResponseBadRequest

                return HttpResponseBadRequest(
                    _("Invalid format type. Use 'json' or 'csv'.")
                )

        except Exception:
            from django.http import HttpResponseServerError

            logger.exception("Database export failed")
            return HttpResponseServerError("Export failed. Check server logs.")

    def _get_export_data(self, from_date=None, to_date=None):
        """
        Extract data using Django ORM, replicating the original polars script logic exactly
        """

        # Get tests (matching original script)
        # `user__isnull` drops try-mode tests: every row here is keyed to a
        # participant (id, gender, education, proficiencies), so an anonymous
        # test has nothing to contribute and used to crash both formats on
        # `test.user.<field>`.
        tests_qs = (
            Test.objects.filter(submitted_at__isnull=False, user__isnull=False)
            .select_related("document__language", "user")
            .prefetch_related(
                Prefetch(
                    "paragraphs",
                    queryset=TestedParagraph.objects.order_by(
                        "answer_ended_at"
                    ).select_related("word__paragraph__language"),
                ),
                "user__academic_fields",
                "user__language_proficiencies__language",
                "document__academic_fields",
            )
        )

        # Apply date filtering if provided
        if from_date:
            tests_qs = tests_qs.filter(submitted_at__date__gte=from_date)
        if to_date:
            tests_qs = tests_qs.filter(submitted_at__date__lte=to_date)

        # Filter out tests without answers (equivalent to original logic)
        # Original: if not set(words["answered_word"]) == {None}
        complete_tests = []
        for test in tests_qs:
            test_paragraphs = test.paragraphs.all()
            answered_words = [tp.answered_word for tp in test_paragraphs]
            # Check if answers contain non-null values (matching original logic)
            # If all answers are None, skip this test
            if set(answered_words) != {None}:
                complete_tests.append(test)

        # Get only documents that are used in the filtered tests
        used_document_ids = {test.document.id for test in complete_tests}
        documents_qs = Document.objects.filter(
            id__in=used_document_ids
        ).prefetch_related("academic_fields", "paragraphs__language")

        return {
            "tests": complete_tests,
            "documents": documents_qs,
        }

    def _export_json(self, export_file_name, from_date=None, to_date=None):
        import json

        from django.http import HttpResponse

        data = self._get_export_data(from_date, to_date)
        export = {"documents": [], "tests": []}

        # Add document data and related paragraphs
        for document in data["documents"]:
            doc_data = {
                "id": document.id,
                "file": str(document.file),
                "title": document.title,
                "language_id": document.language.code,
                "publication_date": str(document.publication_date),
                "uploaded_by_id": document.uploaded_by.id
                if document.uploaded_by
                else None,
                "uploaded_at": str(
                    document.uploaded_at
                ),  # Use str() to match original format
                "authorship_percentage": document.authorship_percentage,
                "author_count": document.author_count,
                "academic_fields": [af.name for af in document.academic_fields.all()]
                or None,  # Use None instead of empty list
                "paragraphs": [],
            }
            # Note: Excluding 'author' and 'released_at' as in original script

            # Add paragraphs for this document
            for paragraph in document.paragraphs.all():
                para_data = {
                    "id": paragraph.id,
                    "content": paragraph.content,
                    "language_id": paragraph.language.code,
                }
                doc_data["paragraphs"].append(para_data)

            export["documents"].append(doc_data)

        # Add test data, answers, and user information
        for test in data["tests"]:
            test_data = {
                "id": str(test.id),
                "document_id": test.document.id,
                "created_at": str(
                    test.created_at
                ),  # Use str() to match original format
                "submitted_at": str(test.submitted_at) if test.submitted_at else None,
                "success_estimate": test.success_estimate,
                "type": test.type,
            }
            # Note: Excluding 'show_preview' as in original script

            # Get answers for this test (prefetched and pre-ordered above)
            answers = []
            for tested_paragraph in test.paragraphs.all():
                answer_data = {
                    "id": tested_paragraph.id,  # TestedParagraph ID
                    "test_id": str(test.id),  # Test ID as string to match original
                    "answered_word": tested_paragraph.answered_word,
                    "content": tested_paragraph.word.content,
                    "answer_started_at": str(tested_paragraph.answer_started_at)
                    if tested_paragraph.answer_started_at
                    else None,
                    "answer_ended_at": str(tested_paragraph.answer_ended_at)
                    if tested_paragraph.answer_ended_at
                    else None,
                    "sentence_blanked": tested_paragraph.word.sentence_blanked,
                    "sentence_index": tested_paragraph.word.sentence_index,
                    "index": tested_paragraph.word.index,
                    "predictions": tested_paragraph.word.predictions,
                    "source": tested_paragraph.word.source,
                    "shape": tested_paragraph.word.shape,
                    "selection_method": tested_paragraph.word.selection_method,
                    "muni_category": tested_paragraph.word.muni_category,
                    "muni_origin_category": tested_paragraph.word.muni_origin_category,
                    "muni_pos_tags": tested_paragraph.word.muni_pos_tags,
                    "word_id": tested_paragraph.word.id,
                    "paragraph_id": tested_paragraph.word.paragraph.id,
                }
                answers.append(answer_data)

            # Get user information
            user = test.user
            user_data = {
                "id": user.id,
                "gender": user.gender,
                "education": user.education,
                "academic_fields": [af.name for af in user.academic_fields.all()]
                or None,  # Use None instead of empty list
                "language_proficiency": [
                    {"language_id": lp.language.code, "proficiency": lp.proficiency}
                    for lp in user.language_proficiencies.all()
                ]
                or None,  # Use None instead of empty list
            }

            export["tests"].append(
                {"test": test_data, "answers": answers, "user": user_data}
            )

        # Create HTTP response with JSON content
        response = HttpResponse(
            json.dumps(export, indent=4, sort_keys=True, default=str),
            content_type="application/json",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{export_file_name}.json"'
        )
        return response

    def _export_csv(self, export_file_name, from_date=None, to_date=None):
        import csv
        from io import StringIO

        from django.http import HttpResponse

        data = self._get_export_data(from_date, to_date)

        # Create CSV content
        output = StringIO()

        # Determine maximum number of answers, languages, and academic fields for CSV headers
        max_answers = 0
        max_languages = 0
        max_academic_fields = 0

        for test in data["tests"]:
            answers_count = test.paragraphs.count()
            max_answers = max(max_answers, answers_count)

            user = test.user
            lang_count = user.language_proficiencies.count()
            max_languages = max(max_languages, lang_count)

            af_count = user.academic_fields.count()
            max_academic_fields = max(max_academic_fields, af_count)

        # Create CSV headers
        headers = [
            "test_id",
            "created_at",
            "submitted_at",
            "success_estimate",
            "test_type",
            "document_language",
            "user_id",
            "gender",
            "education",
        ]

        # Add language headers
        for i in range(1, max_languages + 1):
            headers.extend([f"language_{i}_code", f"language_{i}_proficiency"])

        # Add academic field headers
        for i in range(1, max_academic_fields + 1):
            headers.append(f"academic_field_{i}")

        # Add answer headers
        for i in range(1, max_answers + 1):
            headers.extend(
                [
                    f"answer_{i}_id",
                    f"answer_{i}_test_id",
                    f"answer_{i}_answered_word",
                    f"answer_{i}_content",
                    f"answer_{i}_answer_started_at",
                    f"answer_{i}_answer_ended_at",
                    f"answer_{i}_sentence_blanked",
                    f"answer_{i}_sentence_index",
                    f"answer_{i}_index",
                    f"answer_{i}_predictions",
                    f"answer_{i}_source",
                    f"answer_{i}_shape",
                    f"answer_{i}_selection_method",
                    f"answer_{i}_muni_category",
                    f"answer_{i}_muni_origin_category",
                    f"answer_{i}_muni_pos_tags",
                    f"answer_{i}_word_id",
                    f"answer_{i}_paragraph_id",
                ]
            )

        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()

        # Write data rows
        for test in data["tests"]:
            row = {
                "test_id": str(test.id),
                "created_at": str(
                    test.created_at
                ),  # Use str() to match original format
                "submitted_at": str(test.submitted_at) if test.submitted_at else "",
                "success_estimate": _cell(test.success_estimate),
                "test_type": test.type,
                "document_language": test.document.language.code,
                "user_id": test.user.id,
                "gender": test.user.gender or "",
                "education": test.user.education or "",
            }

            # Add language proficiency data
            language_proficiencies = list(test.user.language_proficiencies.all())
            for i, lp in enumerate(language_proficiencies, 1):
                row[f"language_{i}_code"] = lp.language.code
                row[f"language_{i}_proficiency"] = lp.proficiency

            # Add academic fields data
            academic_fields = list(test.user.academic_fields.all())
            for i, af in enumerate(academic_fields, 1):
                row[f"academic_field_{i}"] = af.name

            # Add answers data (prefetched and pre-ordered)
            answers = list(test.paragraphs.all())
            for i, answer in enumerate(answers, 1):
                row[f"answer_{i}_id"] = answer.id
                row[f"answer_{i}_test_id"] = str(test.id)
                row[f"answer_{i}_answered_word"] = answer.answered_word or ""
                row[f"answer_{i}_content"] = answer.word.content
                row[f"answer_{i}_answer_started_at"] = (
                    str(answer.answer_started_at) if answer.answer_started_at else ""
                )
                row[f"answer_{i}_answer_ended_at"] = (
                    str(answer.answer_ended_at) if answer.answer_ended_at else ""
                )
                row[f"answer_{i}_sentence_blanked"] = answer.word.sentence_blanked or ""
                row[f"answer_{i}_sentence_index"] = _cell(answer.word.sentence_index)
                row[f"answer_{i}_index"] = _cell(answer.word.index)
                row[f"answer_{i}_predictions"] = answer.word.predictions or ""
                row[f"answer_{i}_source"] = answer.word.source or ""
                row[f"answer_{i}_shape"] = answer.word.shape or ""
                row[f"answer_{i}_selection_method"] = answer.word.selection_method or ""
                row[f"answer_{i}_muni_category"] = answer.word.muni_category or ""
                row[f"answer_{i}_muni_origin_category"] = (
                    answer.word.muni_origin_category or ""
                )
                row[f"answer_{i}_muni_pos_tags"] = answer.word.muni_pos_tags or ""
                row[f"answer_{i}_word_id"] = answer.word.id
                row[f"answer_{i}_paragraph_id"] = answer.word.paragraph.id

            writer.writerow(row)

        # Create HTTP response with CSV content
        csv_content = output.getvalue()
        response = HttpResponse(csv_content, content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="{export_file_name}.csv"'
        )
        return response
