from django.shortcuts import redirect
from django.urls import reverse

from core.models import LanguageProficiency


class RestrictAuthenticatedUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.path in [
            reverse("landing-page"),
            reverse("log-in"),
            reverse("sign-up"),
        ]:
            return redirect(reverse("dashboard"))

        response = self.get_response(request)
        return response


class CheckNativeLanguageProficiencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Proceed only if the user is authenticated and not already on the target path
        if (
            request.user.is_authenticated
            and request.path not in [reverse("user-languages"), reverse("log-out")]
            and not request.headers.get("HX-Request")
        ):
            # Check if the user has a native language proficiency
            has_native_proficiency = LanguageProficiency.objects.filter(
                user=request.user, proficiency="native"
            ).exists()

            # Redirect if the user doesn't have a native language proficiency
            if not has_native_proficiency:
                return redirect(reverse("user-languages"))

        # Proceed with the normal response
        response = self.get_response(request)
        return response
