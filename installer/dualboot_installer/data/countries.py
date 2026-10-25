"""Per-country defaults used to pre-fill the regional questions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CountryDefaults:
    locale: str  # glibc locale, e.g. fr_FR.UTF-8
    keymap: str  # kbd console keymap, e.g. fr
    windows_tag: str  # BCP 47 tag, e.g. fr-FR


FALLBACK = CountryDefaults("en_US.UTF-8", "us", "en-US")

COUNTRY_DEFAULTS: dict[str, CountryDefaults] = {
    "AT": CountryDefaults("de_AT.UTF-8", "de-latin1", "de-AT"),
    "AU": CountryDefaults("en_AU.UTF-8", "us", "en-AU"),
    "BE": CountryDefaults("fr_BE.UTF-8", "be-latin1", "fr-BE"),
    "BR": CountryDefaults("pt_BR.UTF-8", "br-abnt2", "pt-BR"),
    "CA": CountryDefaults("en_CA.UTF-8", "us", "en-CA"),
    "CH": CountryDefaults("de_CH.UTF-8", "de_CH-latin1", "de-CH"),
    "CZ": CountryDefaults("cs_CZ.UTF-8", "cz", "cs-CZ"),
    "DE": CountryDefaults("de_DE.UTF-8", "de-latin1", "de-DE"),
    "DK": CountryDefaults("da_DK.UTF-8", "dk", "da-DK"),
    "ES": CountryDefaults("es_ES.UTF-8", "es", "es-ES"),
    "FI": CountryDefaults("fi_FI.UTF-8", "fi", "fi-FI"),
    "FR": CountryDefaults("fr_FR.UTF-8", "fr", "fr-FR"),
    "GB": CountryDefaults("en_GB.UTF-8", "uk", "en-GB"),
    "GR": CountryDefaults("el_GR.UTF-8", "gr", "el-GR"),
    "HU": CountryDefaults("hu_HU.UTF-8", "hu", "hu-HU"),
    "IE": CountryDefaults("en_IE.UTF-8", "uk", "en-IE"),
    "IT": CountryDefaults("it_IT.UTF-8", "it", "it-IT"),
    "JP": CountryDefaults("ja_JP.UTF-8", "jp106", "ja-JP"),
    "KR": CountryDefaults("ko_KR.UTF-8", "us", "ko-KR"),
    "MX": CountryDefaults("es_MX.UTF-8", "la-latin1", "es-MX"),
    "NL": CountryDefaults("nl_NL.UTF-8", "nl", "nl-NL"),
    "NO": CountryDefaults("nb_NO.UTF-8", "no", "nb-NO"),
    "NZ": CountryDefaults("en_NZ.UTF-8", "us", "en-NZ"),
    "PL": CountryDefaults("pl_PL.UTF-8", "pl2", "pl-PL"),
    "PT": CountryDefaults("pt_PT.UTF-8", "pt-latin9", "pt-PT"),
    "RO": CountryDefaults("ro_RO.UTF-8", "ro", "ro-RO"),
    "RU": CountryDefaults("ru_RU.UTF-8", "ru", "ru-RU"),
    "SE": CountryDefaults("sv_SE.UTF-8", "sv-latin1", "sv-SE"),
    "TR": CountryDefaults("tr_TR.UTF-8", "trq", "tr-TR"),
    "UA": CountryDefaults("uk_UA.UTF-8", "ua", "uk-UA"),
    "US": CountryDefaults("en_US.UTF-8", "us", "en-US"),
    "ZA": CountryDefaults("en_ZA.UTF-8", "us", "en-ZA"),
}


def defaults_for(country_code: str) -> CountryDefaults:
    return COUNTRY_DEFAULTS.get(country_code.upper(), FALLBACK)
