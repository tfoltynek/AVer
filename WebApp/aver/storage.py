from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Content-hashing static storage that degrades instead of crashing.

    Default ManifestStaticFilesStorage raises on any ``{% static %}`` reference
    missing from the manifest, which would 500 every page during the short
    window each deploy between the container starting (deploy stage) and
    ``collectstatic`` running (postdeploy stage). With ``manifest_strict =
    False`` an unknown name is served at its original (un-hashed) URL instead —
    pages still render, they just aren't cache-busted for those few seconds.
    """

    manifest_strict = False
