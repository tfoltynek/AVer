import uuid
from pathlib import Path

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Language(models.Model):
    code = models.CharField(
        primary_key=True,
        max_length=35,
    )
    name = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AcademicField(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Education(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")

        user = self.model(
            email=self.normalize_email(email),
        )

        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        user = self.create_user(email, password=password, **extra_fields)
        user.is_admin = True
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    GENDER_CHOICES = [
        ("M", _("Male")),
        ("F", _("Female")),
        ("O", _("Other")),
    ]
    EDUCATION_CHOICES = [
        ("high_school", _("High School Graduation or Lower")),
        ("bachelors", _("Bachelor's Degree")),
        ("masters", _("Master's Degree")),
        ("doctorate", _("Doctorate Degree or Higher")),
    ]

    email = models.EmailField(verbose_name=_("E-mail"), unique=True, null=False)
    gender = models.CharField(
        verbose_name=_("Gender (optional)"),
        max_length=1,
        choices=GENDER_CHOICES,
        blank=True,
        default="",
    )

    education = models.CharField(
        verbose_name=_("Education"),
        max_length=20,
        choices=EDUCATION_CHOICES,
    )
    registered_at = models.DateTimeField(auto_now_add=True)
    academic_fields = models.ManyToManyField(AcademicField, blank=True)

    is_active = models.BooleanField(default=True)
    is_admin = models.BooleanField(default=False)
    can_export_data = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email

    # Intentional: this app doesn't use Django's per-permission system. All
    # permission checks pass; protect endpoints with LoginRequiredMixin and
    # request.user scoping in views instead. Revisit if `@permission_required`
    # or per-object permissions are ever introduced.
    def has_perm(self, perm, obj=None):
        return True

    def has_module_perms(self, app_label):
        return True

    @property
    def is_staff(self):
        "Is the user a member of staff?"
        # Simplest possible answer: All admins are staff
        return self.is_admin


class LanguageProficiency(models.Model):
    PROFICIENCY_CHOICES = [
        ("A1", _("A1 (Beginner)")),
        ("A2", _("A2 (Elementary)")),
        ("B1", _("B1 (Intermediate)")),
        ("B2", _("B2 (Upper Intermediate)")),
        ("C1", _("C1 (Advanced)")),
        ("C2", _("C2 (Proficient)")),
        ("native", _("Native")),
    ]
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="language_proficiencies"
    )
    language = models.ForeignKey(
        Language, on_delete=models.CASCADE, verbose_name=_("Language")
    )
    proficiency = models.CharField(
        verbose_name=_("Proficiency"),
        max_length=6,
        choices=PROFICIENCY_CHOICES,
    )

    def __str__(self):
        return f"{self.user.email} - {self.language.name} ({self.proficiency})"


class Document(models.Model):
    file = models.FileField(upload_to="documents/")
    title = models.CharField(max_length=255)
    language = models.ForeignKey(Language, on_delete=models.CASCADE)
    released_at = models.TextField(blank=True, default="")
    publication_date = models.DateField()
    author = models.TextField(blank=True, default="")
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    authorship_percentage = models.IntegerField(default=100)
    author_count = models.IntegerField(default=1)
    academic_fields = models.ManyToManyField(AcademicField, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    @property
    def cover_name(self):
        "Returns document cover image name."
        return f"core/images/covers/{self.file.name.split('_request.json')[0]}.png"

    @property
    def extension(self):
        file_path = Path(self.file.name)
        return file_path.suffix.lstrip(".")

    @property
    def current_analysis(self):
        """The AI analysis job whose state IS this document's analysis state."""
        return AnalysisJob.objects.current_for(self, "ai")

    @property
    def analysis_state(self) -> str:
        """One value for every reader (views, templates): the current AI
        job's status, or "" when no analysis was ever requested."""
        job = self.current_analysis
        return job.status if job else ""

    def __str__(self):
        return self.title


class Paragraph(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, editable=False, related_name="paragraphs"
    )
    content = models.TextField()
    language = models.ForeignKey(Language, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.document} - {self.content[:25]}"


class Word(models.Model):
    class Source(models.TextChoices):
        MUNI_API = "muni_api", _("MUNI API")
        LOCAL_ANALYZER = "local_analyzer", _("Local analyzer")

    class Shape(models.TextChoices):
        UNIGRAM = "unigram", _("Unigram")
        BIGRAM = "bigram", _("Bigram")
        TRIGRAM = "trigram", _("Trigram")

    @staticmethod
    def shape_for(content: str) -> str:
        """Shape of a pick, from token count of its content.

        Uses whitespace-run splitting like pos_method.classify_method, so
        shape and selection_method always see the same token count. 4+ and
        0 tokens bucket as unigram (none observed in the MUNI contract; a
        real fourth shape would be a schema change).
        """
        n = len((content or "").split())
        if n == 2:
            return Word.Shape.BIGRAM
        if n == 3:
            return Word.Shape.TRIGRAM
        return Word.Shape.UNIGRAM
    # For MUNI words: the granular Hitzinger calibration bucket, mapped at
    # ingestion from the `category` the service reports (POS tagging with
    # stanza is only the fallback when that category is missing or unknown).
    # For local-analyzer words: the pick strategy. Methods present in
    # authorship.SELECTOR_PROBS carry their own pA/pN; anything else (NULL,
    # unmapped categories, other local strategies) is scored with
    # authorship.FALLBACK_PROBS (see authorship.build_items). Note that
    # "random" arrives from both sides — MUNI tops a short document up with
    # randomly removed words, and the local analyzer has a random strategy —
    # so `source` is what tells the two apart.
    #
    # For MUNI words the raw service values are kept verbatim in
    # `muni_category` / `muni_origin_category`, so a later recalibration can
    # be driven from what the service actually said rather than from our
    # mapping of it.
    SELECTION_METHOD_CHOICES = [
        ("ml_noun_unigram", _("Unigram nouns (ML)")),
        ("ml_adj_unigram", _("Unigram adjectives (ML)")),
        ("bigram", _("Bigram (any)")),
        ("adj_adv_trigram", _("Trigrams w/ adjectives + adverbs")),
        ("noun_adj_adv_trigram", _("Trigrams w/ nouns + adjectives + adverbs")),
        ("noun_adj_trigram", _("Trigrams w/ nouns + adjectives")),
        ("trigram_with_adj", _("Trigrams containing adjectives")),
        ("most_used_content_word", _("most_used_content_word")),
        ("least_used_content_word", _("least_used_content_word")),
        ("random", _("random")),
    ]
    paragraph = models.ForeignKey(Paragraph, on_delete=models.CASCADE, editable=False)
    content = models.TextField(editable=False)
    sentence_blanked = models.TextField(null=True, blank=True)
    sentence_index = models.IntegerField(null=True, blank=True)
    index = models.IntegerField(null=True, blank=True)
    predictions = models.JSONField(null=True, blank=True)
    source = models.CharField(
        max_length=30,
        choices=Source.choices,
    )
    shape = models.CharField(
        max_length=30,
        choices=Shape.choices,
    )
    selection_method = models.CharField(
        max_length=30,
        choices=SELECTION_METHOD_CHOICES,
        null=True,
        blank=True,
    )
    expected_embedding = models.BinaryField(null=True, blank=True, editable=False)
    # Verbatim from the MUNI result: the selection class of the word
    # ("unigrams_NOUN", "bigrams_NOUN_ADJ", "random", …), on randomly removed
    # words the class it would otherwise have fallen into, and the Universal
    # POS tag of each of its tokens ("ADJ NOUN NOUN"). The tags settle which
    # calibration group a content-word trigram belongs to, and keeping them
    # means a later recalibration can be driven from the service's own
    # analysis instead of re-tagging the text. NULL on local-analyzer picks
    # and on words ingested before the service reported each field.
    muni_category = models.CharField(
        max_length=40, null=True, blank=True, editable=False
    )
    muni_origin_category = models.CharField(
        max_length=40, null=True, blank=True, editable=False
    )
    muni_pos_tags = models.CharField(
        max_length=120, null=True, blank=True, editable=False
    )

    def __str__(self):
        return f"{self.content}"


class Test(models.Model):
    TEST_TYPE_CHOICES = [
        ("authorML", _("Author")),
        ("previewML", _("Reviewer")),
        ("randomML", _("Tester")),
        ("try", _("Try")),
    ]
    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, editable=False, related_name="tests"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    success_estimate = models.IntegerField(null=True, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    type = models.CharField(
        max_length=30,
        choices=TEST_TYPE_CHOICES,
    )
    show_preview = models.BooleanField(default=False)

    @property
    def answered_paragraphs_count(self):
        return TestedParagraph.objects.filter(
            test=self, answer_ended_at__isnull=False
        ).count()

    @property
    def test_paragraphs_count(self):
        return TestedParagraph.objects.filter(test=self).count()

    @property
    def score(self):
        paragraphs = list(TestedParagraph.objects.filter(test=self))
        if not paragraphs:
            return 0.0
        total = sum(p.computed_score or 0.0 for p in paragraphs)
        return round(total / len(paragraphs) * 100, 2)

    @property
    def scoring_complete(self):
        """True once every paragraph in this test has a computed_score."""
        return not TestedParagraph.objects.filter(
            test=self, computed_score__isnull=True
        ).exists()

    def __str__(self):
        return f"{self.pk} - {self.document}"


class TestedParagraph(models.Model):
    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name="paragraphs",
        related_query_name="paragraph",
    )
    word = models.ForeignKey(Word, on_delete=models.CASCADE)
    answered_word = models.TextField(blank=True, null=True)
    answer_started_at = models.DateTimeField(blank=True, null=True)
    answer_ended_at = models.DateTimeField(blank=True, null=True)
    computed_score = models.FloatField(null=True, blank=True, editable=False)
    computed_grade = models.CharField(max_length=20, blank=True, default="", editable=False)

    @property
    def validate_answer(self):
        if self.answered_word is None:
            return ""
        return self.computed_grade or ""

    def __str__(self):
        return f"{self.pk} - {self.test.pk}"


class AnalysisJobManager(models.Manager):
    def current_for(self, document, analysis_type):
        """The one job that counts for a document + type: the latest created.

        Every reader of "what state is this document's analysis in" goes
        through here (or `Document.analysis_state`), so the definition can't
        drift between the task, the views and the templates.
        """
        return (
            self.filter(document=document, analysis_type=analysis_type)
            .order_by("-created_at")
            .first()
        )


class AnalysisJob(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )
    ANALYSIS_TYPE_CHOICES = (
        ("basic", "Basic analysis"),
        ("ai", "AI analysis"),
        # Add more analysis types here
    )
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="analysis_jobs"
    )
    analysis_type = models.CharField(max_length=50, choices=ANALYSIS_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Stamped by start_analysis: started_at when the worker picks the job up,
    # finished_at on either terminal status. created_at -> started_at is the
    # Celery queue wait; started_at -> finished_at is the analysis itself.
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = AnalysisJobManager()

    # Lifecycle transitions: status and its timestamp always move together,
    # so a new terminal path can't silently skip a stamp.
    def mark_running(self):
        self.status = "running"
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self):
        self.status = "completed"
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "finished_at"])

    def mark_failed(self):
        self.status = "failed"
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "finished_at"])

    @property
    def duration(self):
        """Wall-clock analysis time, or None while pending/running."""
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def __str__(self):
        return f"{self.document} - {self.analysis_type} - {self.status}"


class AnalysisFailureLog(models.Model):
    """Per-call MUNI failure record for debugging flaky/partial analyses.

    Written by `perform_ai_analysis` whenever one of the MUNI API attempts
    (MUNI_API_CALLS of them) fails. The job itself can still end `completed` if other
    attempts succeeded — these rows let you diagnose intermittent issues
    that would otherwise vanish into stdout.

    Auto-pruned by `manage.py prune_analysis_failure_logs` (default 60 days).
    """

    KIND_CHOICES = (
        ("network_error", "Network error"),
        ("http_status", "HTTP non-200 status"),
        ("non_json", "Non-JSON response body"),
        ("bad_submit_response", "202 without a job_id"),
        ("expired", "Job unknown or expired"),
        ("job_failed", "MUNI reported the job failed"),
        ("deadline_exceeded", "Job did not finish in time"),
        ("unhandled", "Unhandled exception"),
    )
    analysis_job = models.ForeignKey(
        AnalysisJob, on_delete=models.CASCADE, related_name="failure_logs"
    )
    attempt = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Which retry attempt failed (1-based). Null for run-wide failures.",
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    summary = models.CharField(max_length=255)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.kind} att={self.attempt}: {self.summary}"
