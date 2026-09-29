from django import forms
from django.contrib.auth import password_validation
from django.utils.translation import gettext_lazy as _

from core.validators import validate_file_size, validate_file_type_and_fix_extension

from .models import AcademicField, Language, LanguageProficiency, User


class AcademicFieldMMCF(forms.ModelMultipleChoiceField):
    def label_from_instance(self, academic_field):
        return academic_field.name


class AcademicFieldsRequiredMixin:
    def clean_academic_fields(self):
        academic_fields = self.cleaned_data.get("academic_fields")
        if not academic_fields:
            raise forms.ValidationError(
                _("You must select at least one field of study."),
                code="missing_academic_field",
            )
        return academic_fields


class LanguageProficiencyForm(forms.ModelForm):
    class Meta:
        model = LanguageProficiency
        fields = ["proficiency", "language"]

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)  # Extract the user from the kwargs
        super().__init__(*args, **kwargs)

        if user:
            # Get languages already added by the user
            existing_languages = LanguageProficiency.objects.filter(
                user=user
            ).values_list("language", flat=True)
            # Exclude the languages already added by the user from the queryset
            self.fields["language"].queryset = Language.objects.exclude(
                pk__in=existing_languages
            )

    def save(self, commit=True, user=None):
        language_proficiency = super().save(commit=False)
        if user:
            language_proficiency.user = user
        if commit:
            language_proficiency.save()
        return language_proficiency


class LanguageProficiencyUpdateForm(forms.ModelForm):
    class Meta:
        model = LanguageProficiency
        fields = ["proficiency"]


class SignUpForm(AcademicFieldsRequiredMixin, forms.ModelForm):
    password = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    password_confirm = forms.CharField(
        label=_("Password confirmation"), widget=forms.PasswordInput
    )
    academic_fields = AcademicFieldMMCF(
        label=_("Academic fields (at least 1)"),
        queryset=AcademicField.objects.all(),
        widget=forms.CheckboxSelectMultiple,
    )

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if password:
            # The account does not exist yet; an unsaved instance lets the
            # similarity validator compare the password with the e-mail.
            password_validation.validate_password(
                password, User(email=self.cleaned_data.get("email", ""))
            )
        return password

    def clean_password_confirm(self):
        # Check that the two password entries match
        password = self.cleaned_data.get("password")
        password_confirm = self.cleaned_data.get("password_confirm")
        if password and password_confirm and password != password_confirm:
            raise forms.ValidationError(
                _("Passwords don't match"), code="passwords_mismatch"
            )
        return password_confirm

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])

        if commit:
            user.save()

        user.academic_fields.set(self.cleaned_data["academic_fields"])
        return user

    class Meta:
        model = User
        fields = ["email", "gender", "education", "academic_fields"]


class DocumentUploadForm(AcademicFieldsRequiredMixin, forms.Form):
    file = forms.FileField(
        label=_("Document file"),
        help_text=_("Upload a document in .txt, .docx, or .pdf format."),
        validators=[validate_file_size, validate_file_type_and_fix_extension],
    )
    academic_fields = AcademicFieldMMCF(
        label=_("Academic fields (at least 1)"),
        help_text=_("Select the academic fields that best match the document."),
        queryset=AcademicField.objects.all(),
        widget=forms.CheckboxSelectMultiple,
    )
    publication_date = forms.DateField(
        label=_("Publication date"),
        help_text=_("Select the date when the document was published or submitted"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    authorship_percentage = forms.IntegerField(
        label=_("Authorship percentage"),
        help_text=_("Indicate your percentage of contribution to the document."),
        min_value=0,
        max_value=100,
        widget=forms.NumberInput(attrs={"type": "range", "value": 100}),
    )
    author_count = forms.IntegerField(
        label=_("Number of authors"),
        min_value=1,
        initial=1,
        help_text=_("Specify how many authors worked on this document."),
    )


class TestSuccessEstimateForm(forms.Form):
    success_estimate = forms.IntegerField(
        label=_("Success estimate"),
        help_text=_("Estimate how well you think you performed on the test."),
        min_value=0,
        max_value=100,
        initial=50,
        widget=forms.NumberInput(attrs={"type": "range", "value": 100}),
    )


class UpdateAcademicFieldsForm(AcademicFieldsRequiredMixin, forms.Form):
    academic_fields = AcademicFieldMMCF(
        label=_("Academic fields (at least 1)"),
        queryset=AcademicField.objects.all(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user:
            self.fields["academic_fields"].initial = user.academic_fields.all()


class PasswordChangeForm(forms.Form):
    old_password = forms.CharField(
        label=_("Current password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text=_("Enter your current password to confirm the change."),
    )
    new_password = forms.CharField(
        label=_("New password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=_("Must have at least 10 characters."),
    )
    new_password_confirm = forms.CharField(
        label=_("Confirm new password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

    def clean_old_password(self):
        old_password = self.cleaned_data.get("old_password")
        if self.user and not self.user.check_password(old_password):
            raise forms.ValidationError(
                _("Your current password is incorrect."), code="password_incorrect"
            )
        return old_password

    def clean_new_password_confirm(self):
        new_password = self.cleaned_data.get("new_password")
        new_password_confirm = self.cleaned_data.get("new_password_confirm")
        if new_password and new_password_confirm and new_password != new_password_confirm:
            raise forms.ValidationError(
                _("Passwords don't match"), code="passwords_mismatch"
            )
        return new_password_confirm

    def clean_new_password(self):
        new_password = self.cleaned_data.get("new_password")
        if new_password:
            password_validation.validate_password(new_password, self.user)
        return new_password

    def save(self):
        if self.user:
            self.user.set_password(self.cleaned_data["new_password"])
            self.user.save()
        return self.user
