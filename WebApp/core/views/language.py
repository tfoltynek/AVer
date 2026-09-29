from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from core.forms import (
    LanguageProficiencyForm,
    LanguageProficiencyUpdateForm,
)
from core.models import LanguageProficiency


class LanguageProficiencyListView(LoginRequiredMixin, View):
    def get(self, request):
        template = (
            "core/partials/language-proficiency-list.html"
            if request.headers.get("HX-Request")
            else "core/language-proficiency.html"
        )
        language_list = LanguageProficiency.objects.filter(user=self.request.user)
        form = LanguageProficiencyForm(user=request.user)
        return render(
            request,
            template,
            {"form": form, "language_list": language_list},
        )

    def post(self, request):
        template = (
            "core/partials/language-proficiency-list.html"
            if request.headers.get("HX-Request")
            else "core/language-proficiency.html"
        )
        language_list = LanguageProficiency.objects.filter(user=self.request.user)

        form = LanguageProficiencyForm(data=request.POST)
        if form.is_valid():
            form.save(user=request.user)
            language_list = LanguageProficiency.objects.filter(user=self.request.user)
            form = LanguageProficiencyForm(user=request.user)

        return render(
            request,
            template,
            {"form": form, "language_list": language_list},
        )


class LanguageProficiencyDetailView(LoginRequiredMixin, View):
    def get(self, request, pk):
        template = "core/partials/language-proficiency-item.html"
        language = get_object_or_404(LanguageProficiency, pk=pk, user=self.request.user)

        return render(request, template, {"lang": language})


class LanguageProficiencyUpdateView(LoginRequiredMixin, View):
    def get(self, request, pk):
        template = "core/partials/language-proficiency-edit.html"
        language = get_object_or_404(LanguageProficiency, pk=pk, user=self.request.user)
        form = LanguageProficiencyUpdateForm(instance=language)

        return render(
            request,
            template,
            {"form": form, "lang": language},
        )

    def post(self, request, pk):
        template = "core/partials/language-proficiency-item.html"
        language = get_object_or_404(LanguageProficiency, pk=pk, user=self.request.user)

        form = LanguageProficiencyUpdateForm(data=request.POST)
        if form.is_valid():
            language.proficiency = form.cleaned_data["proficiency"]
            language.save()
        else:
            template = "core/partials/language-proficiency-edit.html"

        return render(
            request,
            template,
            {"form": form, "lang": language},
        )


class LanguageProficiencyDeleteView(LoginRequiredMixin, View):
    # POST-only on purpose: deletion is destructive and a GET handler is not
    # CSRF-protected, so a cross-site link could silently remove a user's row.
    def post(self, request, pk):
        language = get_object_or_404(LanguageProficiency, pk=pk, user=self.request.user)
        language.delete()
        if request.headers.get("HX-Request"):
            language_list = LanguageProficiency.objects.filter(user=request.user)
            from core.forms import LanguageProficiencyForm

            form = LanguageProficiencyForm(user=request.user)
            return render(
                request,
                "core/partials/language-proficiency-list.html",
                {"form": form, "language_list": language_list},
            )
        return redirect(reverse("user-languages"))
