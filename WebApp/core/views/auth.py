from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.generic import View

from core.forms import (
    PasswordChangeForm,
    SignUpForm,
    UpdateAcademicFieldsForm,
)


class SignUpView(View):
    def get(self, request, *args, **kwargs):
        form = SignUpForm()
        return render(request, "core/sign-up.html", {"form": form})

    def post(self, request, *args, **kwargs):
        template = (
            "core/partials/sign-up-form.html"
            if request.headers.get("HX-Request")
            else "core/sign-up.html"
        )
        form = SignUpForm(request.POST)
        if form.is_valid():
            login(request, form.save())
            response = HttpResponse()
            response["HX-Redirect"] = reverse("dashboard")
            return response

        return render(request, template, {"form": form})


class LogInView(View):
    def get(self, request, *args, **kwargs):
        form = AuthenticationForm()
        return render(request, "core/log-in.html", {"form": form})

    def post(self, request, *args, **kwargs):
        template = (
            "core/partials/log-in-form.html"
            if request.headers.get("HX-Request")
            else "core/log-in.html"
        )
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            response = HttpResponse()
            response["HX-Redirect"] = reverse("dashboard")
            return response

        return render(request, template, {"form": form})


class LogOutView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        logout(request)
        return redirect(reverse("log-in"))


class PasswordChangeView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        form = PasswordChangeForm(user=request.user)
        context = {"form": form}
        if request.headers.get("HX-Request"):
            return render(
                request, "core/partials/password-change-dialog.html", context
            )
        return render(request, "core/change_password.html", context)

    def post(self, request, *args, **kwargs):
        form = PasswordChangeForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            # Rotate the session auth hash so set_password() does not log the
            # user out on the next request.
            update_session_auth_hash(request, request.user)
            if request.headers.get("HX-Request"):
                response = render(
                    request, "core/partials/password-change-success.html"
                )
                response["HX-Trigger"] = "closePasswordDialog"
                return response
            return redirect(reverse("settings"))

        context = {"form": form}
        if request.headers.get("HX-Request"):
            return render(
                request, "core/partials/password-change-form.html", context
            )
        return render(request, "core/change_password.html", context)


class UpdateAcademicFieldsView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        form = UpdateAcademicFieldsForm(user=request.user)
        context = {"form": form, "user": request.user}
        if request.headers.get("HX-Request"):
            return render(
                request, "core/partials/academic-fields-dialog.html", context
            )
        return render(request, "core/update_academic_fields.html", context)

    def post(self, request, *args, **kwargs):
        user = request.user
        form = UpdateAcademicFieldsForm(request.POST, user=user)

        if form.is_valid():
            user.academic_fields.set(form.cleaned_data["academic_fields"])
            if request.headers.get("HX-Request"):
                response = render(
                    request,
                    "core/partials/academic-fields-update-success.html",
                    {"user": user},
                )
                response["HX-Trigger"] = "closeAcademicFieldsDialog"
                return response
            return redirect(reverse("settings"))
        context = {"form": form, "user": user}
        if request.headers.get("HX-Request"):
            return render(
                request, "core/partials/update-academic-fields-form.html", context
            )
        return render(request, "core/update_academic_fields.html", context)
