import functools
from pathlib import Path

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from magika import Magika

# Allowed MIME types and their correct extensions
ALLOWED_MIME_TYPES = {
    'application/pdf': '.pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'text/plain': '.txt'
}

MAX_UPLOAD_SIZE = 30 * 1024 * 1024  # 30 MB


@functools.cache
def _magika():
    return Magika()


def validate_file_size(file_field):
    if file_field.size > MAX_UPLOAD_SIZE:
        raise ValidationError(
            _("File is too large (%(size)s MB). The limit is %(limit)s MB.") % {
                "size": file_field.size // (1024 * 1024),
                "limit": MAX_UPLOAD_SIZE // (1024 * 1024),
            }
        )


class MinimumLengthValidator(password_validation.MinimumLengthValidator):
    """Django's length validator with a message from this project's catalogue.

    Django 6.0 changed the stock message's msgid to "%d" but its Czech
    catalogue still carries the old "%(min_length)d" entry, so the stock
    validator reaches Czech users in English. Owning the sentence keeps it in
    locale/, where makemessages and the translation test see it.
    """

    def validate(self, password, user=None):
        if len(password) < self.min_length:
            raise ValidationError(
                _("The password must have at least %(min_length)d characters."),
                code="password_too_short",
                params={"min_length": self.min_length},
            )

    def get_help_text(self):
        return _("The password must have at least %(min_length)d characters.") % {
            "min_length": self.min_length
        }


def validate_file_type_and_fix_extension(file_field):
    """
    Validator using Magika to check if a file is PDF, TXT, or DOCX, and fix the file extension if incorrect.
    """
    file_bytes = file_field.read()
    result = _magika().identify_bytes(file_bytes)
    
    # Get the MIME type and file label
    mime_type = result.output.mime_type
    label = result.output.label
    
    # Check if the MIME type is allowed
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            _("Unsupported file type '%(label)s'. Only PDF, DOCX, or TXT files are allowed.") % {"label": label}
        )
    
    # Strip path components and ensure correct extension
    file_path = Path(file_field.name)
    correct_extension = ALLOWED_MIME_TYPES[mime_type]
    file_field.name = file_path.with_suffix(correct_extension).name

    # Reset file pointer after reading it
    file_field.seek(0)