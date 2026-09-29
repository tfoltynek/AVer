from django.conf import settings
from django.urls import include, path

from .views import auth, cloze, documents, language, pages

urlpatterns = [
    path("healthz", pages.HealthCheckView.as_view(), name="healthz"),
    path("", pages.LandingPageView.as_view(), name="landing-page"),
    path("data", pages.DataPageView.as_view(), name="data-page"),
    path("sign-up", auth.SignUpView.as_view(), name="sign-up"),
    path("log-in", auth.LogInView.as_view(), name="log-in"),
    path("log-out", auth.LogOutView.as_view(), name="log-out"),
    path(
        "user-languages",
        language.LanguageProficiencyListView.as_view(),
        name="user-languages",
    ),
    path(
        "user-languages/<int:pk>",
        language.LanguageProficiencyDetailView.as_view(),
        name="user-language-card",
    ),
    path(
        "user-languages/<int:pk>/update",
        language.LanguageProficiencyUpdateView.as_view(),
        name="user-language-update",
    ),
    path(
        "user-languages/<int:pk>/delete",
        language.LanguageProficiencyDeleteView.as_view(),
        name="user-language-delete",
    ),
    path(
        "user/update-academic-fields",
        auth.UpdateAcademicFieldsView.as_view(),
        name="update-academic-fields",
    ),
    path(
        "user/change-password",
        auth.PasswordChangeView.as_view(),
        name="change-password",
    ),
    path("dashboard", pages.DashboardView.as_view(), name="dashboard"),
    path("settings", pages.SettingsView.as_view(), name="settings"),
    path(
        "settings/service-status",
        pages.ServiceStatusView.as_view(),
        name="service-status",
    ),
    path(
        "settings/export/<str:format_type>",
        pages.DatabaseExportView.as_view(),
        name="database-export",
    ),
    path(
        "settings/export",
        pages.DatabaseExportView.as_view(),
        name="database-export-default",
    ),
    path("documents", documents.DocumentListView.as_view(), name="document-list"),
    path(
        "documents/<int:pk>/delete",
        documents.DocumentDeleteView.as_view(),
        name="document-delete",
    ),
    path(
        "documents/upload",
        documents.DocumentUploadView.as_view(),
        name="document-upload",
    ),
    path(
        "documents/<int:document_id>/test",
        cloze.CreateDocumentTestView.as_view(),
        name="create-document-test",
    ),
    path("test/select", cloze.TestSelectView.as_view(), name="test-select"),
    path("test/select/<str:type>", cloze.TestCreateView.as_view(), name="test-create"),
    path("test/<str:test_id>", cloze.DocumentTestView.as_view(), name="document-test"),
    path(
        "try/documents/<str:language>",
        documents.TryDocumentListView.as_view(),
        name="try-document-select",
    ),
    path(
        "try/documents/<str:document_id>/test",
        cloze.create_try_document_test,
        name="create-try-document-test",
    ),
    path(
        "test/<str:test_id>/skip",
        cloze.DocumentTestSkipView.as_view(),
        name="document-test-skip",
    ),
    path(
        "test/<str:test_id>/success-estimate",
        cloze.DocumentTestSuccessEstimateView.as_view(),
        name="test-success-estimate",
    ),
    path(
        "test/<str:test_id>/preview",
        cloze.DocumentTestPreviewView.as_view(),
        name="test-preview",
    ),
    path("tests", cloze.TestListView.as_view(), name="test-list"),
    path("i18n/", include("django.conf.urls.i18n")),
]


if not settings.TESTING:
    from debug_toolbar.toolbar import debug_toolbar_urls

    urlpatterns = [
        *urlpatterns,
    ] + debug_toolbar_urls()
