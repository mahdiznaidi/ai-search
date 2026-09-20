from __future__ import annotations

import logging

import pandas as pd

from stagefinder.models import RawJob

log = logging.getLogger(__name__)

# ISO 3166-1 alpha-2 → nom accepté par jobspy.model.Country.from_string
INDEED_COUNTRIES = {
    "AR": "argentina", "AU": "australia", "AT": "austria", "BH": "bahrain", "BD": "bangladesh",
    "BE": "belgium", "BG": "bulgaria", "BR": "brazil", "CA": "canada", "CL": "chile",
    "CN": "china", "CO": "colombia", "CR": "costa rica", "HR": "croatia", "CY": "cyprus",
    "CZ": "czech republic", "DK": "denmark", "EC": "ecuador", "EG": "egypt", "EE": "estonia",
    "FI": "finland", "FR": "france", "DE": "germany", "GR": "greece", "HK": "hong kong",
    "HU": "hungary", "IN": "india", "ID": "indonesia", "IE": "ireland", "IL": "israel",
    "IT": "italy", "JP": "japan", "KW": "kuwait", "LV": "latvia", "LT": "lithuania",
    "LU": "luxembourg", "MY": "malaysia", "MT": "malta", "MX": "mexico", "MA": "morocco",
    "NL": "netherlands", "NZ": "new zealand", "NG": "nigeria", "NO": "norway", "OM": "oman",
    "PK": "pakistan", "PA": "panama", "PE": "peru", "PH": "philippines", "PL": "poland",
    "PT": "portugal", "QA": "qatar", "RO": "romania", "SA": "saudi arabia", "SG": "singapore",
    "SK": "slovakia", "SI": "slovenia", "ZA": "south africa", "KR": "south korea",
    "ES": "spain", "SE": "sweden", "CH": "switzerland", "TW": "taiwan", "TH": "thailand",
    "TR": "turkey", "UA": "ukraine", "AE": "united arab emirates", "GB": "uk", "US": "usa",
    "UY": "uruguay", "VE": "venezuela", "VN": "vietnam",
}


def _default_scrape(**kw):
    from jobspy import scrape_jobs
    return scrape_jobs(**kw)


def rows_to_rawjobs(df: pd.DataFrame) -> list[RawJob]:
    def s(v) -> str:
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):  # listes/arrays : pd.isna non scalaire
            pass
        return str(v)

    out = []
    for _, r in df.iterrows():
        dp = r.get("date_posted")
        posted = None if dp is None or pd.isna(dp) else str(pd.Timestamp(dp).date())
        out.append(RawJob(source=s(r.get("site")), external_id=s(r.get("id")) or None,
                          title=s(r.get("title")), company=s(r.get("company")),
                          location=s(r.get("location")), url=s(r.get("job_url")),
                          description=s(r.get("description")), posted_at=posted))
    return out


class JobSpySource:
    name = "jobspy"

    def __init__(self, sites=("indeed", "linkedin"), scrape=_default_scrape):
        self.sites = list(sites)
        self._scrape = scrape

    def supports(self, country: str) -> bool:
        return True  # LinkedIn accepte toute localisation ; Indeed est filtré dans search()

    def search(self, keyword, location, country, job_type, hours_old, is_remote, limit):
        country = country.upper()
        sites = [x for x in self.sites if x != "indeed" or country in INDEED_COUNTRIES]
        if not sites:
            return []
        kw = dict(site_name=sites, search_term=keyword, location=location,
                  results_wanted=limit, hours_old=hours_old, is_remote=is_remote,
                  job_type=job_type, country_indeed=INDEED_COUNTRIES.get(country, "usa"),
                  linkedin_fetch_description=True, verbose=0)
        try:
            df = self._scrape(**kw)
            if df is None or df.empty:
                return []
            return rows_to_rawjobs(df)
        except Exception as e:  # le scraper tiers lève des exceptions variées ; une ligne malformée aussi
            log.warning("jobspy %r @ %r: %s", keyword, location, e, exc_info=True)
            return []
