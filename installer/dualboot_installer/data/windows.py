"""Windows 11 reference data: languages, editions, keyboards and time zones."""

from __future__ import annotations

from dataclasses import dataclass

from .countries import defaults_for


@dataclass(frozen=True)
class WindowsLanguage:
    name: str  # language name as listed on Microsoft's download page
    tag: str  # BCP 47 tag used in the unattend file


LANGUAGES: tuple[WindowsLanguage, ...] = (
    WindowsLanguage("Arabic", "ar-SA"),
    WindowsLanguage("Bulgarian", "bg-BG"),
    WindowsLanguage("Chinese Simplified", "zh-CN"),
    WindowsLanguage("Chinese Traditional", "zh-TW"),
    WindowsLanguage("Croatian", "hr-HR"),
    WindowsLanguage("Czech", "cs-CZ"),
    WindowsLanguage("Danish", "da-DK"),
    WindowsLanguage("Dutch", "nl-NL"),
    WindowsLanguage("English (United States)", "en-US"),
    WindowsLanguage("English International", "en-GB"),
    WindowsLanguage("Estonian", "et-EE"),
    WindowsLanguage("Finnish", "fi-FI"),
    WindowsLanguage("French", "fr-FR"),
    WindowsLanguage("French Canadian", "fr-CA"),
    WindowsLanguage("German", "de-DE"),
    WindowsLanguage("Greek", "el-GR"),
    WindowsLanguage("Hebrew", "he-IL"),
    WindowsLanguage("Hungarian", "hu-HU"),
    WindowsLanguage("Italian", "it-IT"),
    WindowsLanguage("Japanese", "ja-JP"),
    WindowsLanguage("Korean", "ko-KR"),
    WindowsLanguage("Latvian", "lv-LV"),
    WindowsLanguage("Lithuanian", "lt-LT"),
    WindowsLanguage("Norwegian", "nb-NO"),
    WindowsLanguage("Polish", "pl-PL"),
    WindowsLanguage("Portuguese", "pt-PT"),
    WindowsLanguage("Portuguese (Brazil)", "pt-BR"),
    WindowsLanguage("Romanian", "ro-RO"),
    WindowsLanguage("Russian", "ru-RU"),
    WindowsLanguage("Serbian Latin", "sr-Latn-RS"),
    WindowsLanguage("Slovak", "sk-SK"),
    WindowsLanguage("Slovenian", "sl-SI"),
    WindowsLanguage("Spanish", "es-ES"),
    WindowsLanguage("Spanish (Mexico)", "es-MX"),
    WindowsLanguage("Swedish", "sv-SE"),
    WindowsLanguage("Thai", "th-TH"),
    WindowsLanguage("Turkish", "tr-TR"),
    WindowsLanguage("Ukrainian", "uk-UA"),
)

DEFAULT_LANGUAGE = next(lang for lang in LANGUAGES if lang.tag == "en-US")

EDITIONS: tuple[str, ...] = ("Pro", "Home")

# (label, language tag). A language tag selects that language's default
# keyboard layout in the unattend file (InputLocale accepts tags).
KEYBOARDS: tuple[tuple[str, str], ...] = (
    ("US", "en-US"),
    ("United Kingdom", "en-GB"),
    ("French (AZERTY)", "fr-FR"),
    ("French (Belgium)", "fr-BE"),
    ("French (Canada)", "fr-CA"),
    ("French (Switzerland)", "fr-CH"),
    ("German", "de-DE"),
    ("German (Switzerland)", "de-CH"),
    ("German (Austria)", "de-AT"),
    ("Spanish", "es-ES"),
    ("Spanish (Latin America)", "es-MX"),
    ("Italian", "it-IT"),
    ("Portuguese", "pt-PT"),
    ("Portuguese (Brazil ABNT2)", "pt-BR"),
    ("Dutch", "nl-NL"),
    ("Swedish", "sv-SE"),
    ("Norwegian", "nb-NO"),
    ("Danish", "da-DK"),
    ("Finnish", "fi-FI"),
    ("Polish", "pl-PL"),
    ("Czech", "cs-CZ"),
    ("Hungarian", "hu-HU"),
    ("Romanian", "ro-RO"),
    ("Greek", "el-GR"),
    ("Russian", "ru-RU"),
    ("Ukrainian", "uk-UA"),
    ("Turkish (Q)", "tr-TR"),
    ("Japanese", "ja-JP"),
    ("Korean", "ko-KR"),
)

# Linux console keymap -> Windows language tag, for keymaps whose name does
# not already match the country default.
KEYMAP_TO_WINDOWS: dict[str, str] = {
    "us": "en-US",
    "uk": "en-GB",
    "fr": "fr-FR",
    "fr-latin9": "fr-FR",
    "be-latin1": "fr-BE",
    "cf": "fr-CA",
    "fr_CH": "fr-CH",
    "fr_CH-latin1": "fr-CH",
    "de": "de-DE",
    "de-latin1": "de-DE",
    "de_CH-latin1": "de-CH",
    "es": "es-ES",
    "la-latin1": "es-MX",
    "it": "it-IT",
    "pt-latin9": "pt-PT",
    "br-abnt2": "pt-BR",
    "nl": "nl-NL",
    "sv-latin1": "sv-SE",
    "no": "nb-NO",
    "dk": "da-DK",
    "fi": "fi-FI",
    "pl2": "pl-PL",
    "cz": "cs-CZ",
    "hu": "hu-HU",
    "ro": "ro-RO",
    "gr": "el-GR",
    "ru": "ru-RU",
    "ua": "uk-UA",
    "trq": "tr-TR",
    "jp106": "ja-JP",
}

# Used only when python-tzlocal (CLDR data) is unavailable.
_FALLBACK_TIMEZONES: dict[str, str] = {
    "UTC": "UTC",
    "Europe/London": "GMT Standard Time",
    "Europe/Dublin": "GMT Standard Time",
    "Europe/Lisbon": "GMT Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Brussels": "Romance Standard Time",
    "Europe/Madrid": "Romance Standard Time",
    "Europe/Copenhagen": "Romance Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Zurich": "W. Europe Standard Time",
    "Europe/Rome": "W. Europe Standard Time",
    "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Vienna": "W. Europe Standard Time",
    "Europe/Stockholm": "W. Europe Standard Time",
    "Europe/Oslo": "W. Europe Standard Time",
    "Europe/Prague": "Central Europe Standard Time",
    "Europe/Budapest": "Central Europe Standard Time",
    "Europe/Warsaw": "Central European Standard Time",
    "Europe/Helsinki": "FLE Standard Time",
    "Europe/Athens": "GTB Standard Time",
    "Europe/Istanbul": "Turkey Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Toronto": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time",
    "America/Mexico_City": "Central Standard Time (Mexico)",
    "Asia/Tokyo": "Tokyo Standard Time",
    "Asia/Seoul": "Korea Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "Pacific/Auckland": "New Zealand Standard Time",
}


def windows_timezone(iana_name: str) -> str:
    """Map an IANA time zone (Europe/Paris) to a Windows time zone ID."""
    try:
        from tzlocal.windows_tz import tz_win

        found = tz_win.get(iana_name)
        if found:
            return found
    except ImportError:
        pass
    return _FALLBACK_TIMEZONES.get(iana_name, "UTC")


def default_language(country_code: str) -> WindowsLanguage:
    """Pick the Windows ISO language that best matches a country."""
    tag = defaults_for(country_code).windows_tag
    for lang in LANGUAGES:
        if lang.tag.lower() == tag.lower():
            return lang
    prefix = tag.split("-")[0].lower()
    for lang in LANGUAGES:
        if lang.tag.split("-")[0].lower() == prefix:
            return lang
    return DEFAULT_LANGUAGE


def language_by_tag(tag: str) -> WindowsLanguage:
    """The language with this BCP 47 tag; an unlisted tag is kept as is."""
    for lang in LANGUAGES:
        if lang.tag.lower() == tag.lower():
            return lang
    return WindowsLanguage(tag, tag)


def language_by_name(name: str) -> WindowsLanguage:
    for lang in LANGUAGES:
        if lang.name == name:
            return lang
    return DEFAULT_LANGUAGE


def default_keyboard(keymap: str, country_code: str) -> str:
    """Windows keyboard tag matching a Linux console keymap (or the country)."""
    tag = KEYMAP_TO_WINDOWS.get(keymap)
    if tag:
        return tag
    return defaults_for(country_code).windows_tag
