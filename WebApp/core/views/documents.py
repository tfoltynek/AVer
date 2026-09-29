from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.forms import ValidationError
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import DeleteView, View

from core.forms import DocumentUploadForm
from core.ingestion import (
    OversizedDocument,
    UnsupportedLanguage,
    UnusableDocument,
    ingest_upload,
)
from core.models import Document


class TryDocumentListView(View):
    def get(self, request, *args, **kwargs):
        documents = Document.objects.filter(
            language=kwargs["language"], uploaded_by__isnull=True
        ).order_by("title")

        return render(
            request,
            "core/try_document_select.html",
            {
                "document_list": documents,
                "active_language": kwargs["language"],
                "language_options": [
                    {"code": "cs", "label": "Čeština"},
                    {"code": "sk", "label": "Slovenčina"},
                    {"code": "en", "label": "English"},
                ],
            },
        )


class DocumentListView(LoginRequiredMixin, View):
    def get(self, request):
        object_list = Document.objects.filter(uploaded_by=self.request.user).order_by(
            "-uploaded_at"
        )
        if request.headers.get("HX-Request"):
            return render(
                request,
                "core/partials/document_list.html",
                {"object_list": object_list},
            )
        else:
            return render(request, "core/documents.html", {"object_list": object_list})


class DocumentUploadView(LoginRequiredMixin, View):
    def get(self, request):
        form = DocumentUploadForm()
        return render(request, "core/document_upload.html", {"form": form})

    def post(self, request, *args, **kwargs):
        form = DocumentUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return self._render_form(request, form)

        try:
            ingest_upload(
                form.cleaned_data["file"],
                metadata=form.cleaned_data,
                user=request.user,
            )
        except UnsupportedLanguage:
            form.add_error(
                "file",
                ValidationError(
                    _(
                        "The language of the document could not be recognized. "
                        "Supported languages are Czech, Slovak and English."
                    )
                ),
            )
            return self._render_form(request, form)
        except OversizedDocument as exc:
            form.add_error(
                "file",
                ValidationError(
                    _(
                        "The PDF has %(pages)s pages; the limit is %(limit)s. "
                        "Split it into parts or upload a shorter text."
                    )
                    % {"pages": exc.pages, "limit": exc.limit}
                ),
            )
            return self._render_form(request, form)
        except UnusableDocument:
            form.add_error(
                "file",
                ValidationError(
                    _(
                        "The document content is not usable for this application. "
                        "Try a longer document or a different file."
                    )
                ),
            )
            return self._render_form(request, form)

        messages.success(
            request,
            _("Your document was successfully added and is now being analyzed."),
        )
        response = HttpResponse()
        response["HX-Redirect"] = reverse("dashboard")
        return response

    def _render_form(self, request, form):
        template = (
            "core/partials/document-upload-form.html"
            if request.headers.get("HX-Request")
            else "core/document_upload.html"
        )
        return render(request, template, {"form": form})


class DocumentDeleteView(LoginRequiredMixin, DeleteView):
    model = Document
    success_url = reverse_lazy("document-list")

    def get_queryset(self):
        # Scope deletion to the requesting user's own documents — anything
        # else 404s rather than leaking existence of other users' docs.
        return super().get_queryset().filter(uploaded_by=self.request.user)
