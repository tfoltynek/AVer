from lingua import Language, LanguageDetectorBuilder

detector = LanguageDetectorBuilder.from_languages(
    *[Language.ENGLISH, Language.CZECH, Language.SLOVAK]
).build()


def detect_language(text: str) -> str | None:
    language = detector.detect_language_of(text)

    if language is None:
        return None

    return language.iso_code_639_1.name.lower()
