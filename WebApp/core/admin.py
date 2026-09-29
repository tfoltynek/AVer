from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import (
    AcademicField,
    AnalysisFailureLog,
    AnalysisJob,
    Document,
    LanguageProficiency,
    Paragraph,
    Test,
    TestedParagraph,
    User,
    Word,
)


# Register your models here.
class UserCreationForm(forms.ModelForm):
    password1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    password2 = forms.CharField(
        label=_("Password confirmation"), widget=forms.PasswordInput
    )

    class Meta:
        model = User
        fields = ["email"]

    def clean_password2(self):
        # Check that the two password entries match
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError(_("Passwords don't match"))
        return password2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class UserChangeForm(forms.ModelForm):
    password = ReadOnlyPasswordHashField()

    class Meta:
        model = User
        fields = ["email", "password", "is_active", "is_admin"]


class UserAdmin(BaseUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm

    list_display = ["email", "is_admin", "gender", "education"]
    list_filter = ["is_admin"]
    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        (_("Personal info"), {"fields": ["gender", "education", "academic_fields"]}),
        (_("Permissions"), {"fields": ["is_admin", "is_active", "can_export_data"]}),
    ]
    # add_fieldsets is not a standard ModelAdmin attribute. UserAdmin
    # overrides get_fieldsets to use this attribute when creating a user.
    add_fieldsets = [
        (
            None,
            {
                "classes": ["wide"],
                "fields": ["email", "password1", "password2"],
            },
        ),
    ]
    search_fields = ["email"]
    ordering = ["email"]
    filter_horizontal = ["academic_fields"]


admin.site.register(User, UserAdmin)
admin.site.unregister(Group)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "id",
        "file",
        "language",
        "released_at",
        "publication_date",
        "uploaded_at",
        "uploaded_by",
        "author",
    ]
    list_filter = ["language", "uploaded_at", "uploaded_by"]
    date_hierarchy = "uploaded_at"


@admin.register(Paragraph)
class ParagraphAdmin(admin.ModelAdmin):
    list_display = ["document", "content"]


@admin.register(Word)
class WordAdmin(admin.ModelAdmin):
    list_display = [
        "paragraph",
        "content",
        "source",
        "shape",
        "selection_method",
        "muni_category",
    ]
    list_filter = [
        "source",
        "shape",
        "selection_method",
        "muni_category",
        "paragraph__document",
    ]
    search_fields = ["content", "paragraph__document__title"]

    readonly_fields = [
        "paragraph",
        "content",
        "sentence_blanked",
        "sentence_index",
        "predictions",
        "muni_category",
        "muni_origin_category",
        "muni_pos_tags",
    ]


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "document",
        "submitted_at",
        "score",
        "test_paragraphs_count",
        "user",
        "created_at",
        "type",
    ]


@admin.register(TestedParagraph)
class TestedParagraphAdmin(admin.ModelAdmin):
    list_display = [
        "test",
        "word",
        "answered_word",
        "answer_started_at",
        "answer_ended_at",
    ]


@admin.register(AcademicField)
class AcademicFieldAdmin(admin.ModelAdmin):
    list_display = ["name"]


@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "document",
        "analysis_type",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "duration",
        "failure_count",
    ]
    list_filter = ["status", "analysis_type"]

    @admin.display(description="failures")
    def failure_count(self, obj: AnalysisJob) -> int:
        return obj.failure_logs.count()


@admin.register(AnalysisFailureLog)
class AnalysisFailureLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "analysis_job", "kind", "attempt", "summary"]
    list_filter = ["kind", "created_at"]
    search_fields = ["summary", "analysis_job__document__title"]
    readonly_fields = [
        "analysis_job", "attempt", "kind", "summary", "details", "created_at",
    ]
    date_hierarchy = "created_at"


@admin.register(LanguageProficiency)
class LanguageProficiencyAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "language",
        "proficiency",
    ]
