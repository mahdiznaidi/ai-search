from __future__ import annotations

import logging
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from stagefinder.models import RawJob

log = logging.getLogger(__name__)
BASE = "https://www.jobteaser.com"
EU = {"FR", "DE", "ES", "IT", "BE", "NL", "LU", "CH", "AT", "PT", "PL", "SE", "DK", "FI",
      "NO", "IE", "GB", "CZ"}


def parse_jobteaser(html: str, location: str) -> list[RawJob]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".job-offer-card"):
        title = card.select_one(".job-offer-card__title")
        link = card.select_one("a[href]")
        if not title or not link:
            continue
        href = link["href"]
        url = href if href.startswith("http") else BASE + href
        # Extract external_id from href (last part after the last /)
        external_id = href.rstrip("/").split("/")[-1] if href else None
        company = card.select_one(".job-offer-card__company")
        out.append(RawJob(source="jobteaser", external_id=external_id,
                          title=title.get_text(strip=True),
                          company=company.get_text(strip=True) if company else "",
                          location=location, url=url))
    return out


class JobTeaserSource:
    name = "jobteaser"

    def __init__(self, get=requests.get):
        self._get = get

    def supports(self, country: str) -> bool:
        return country.upper() in EU

    def search(self, keyword, location, country, job_type, hours_old, is_remote, limit):
        url = f"{BASE}/fr/job-offers?query={quote_plus(keyword)}&location={quote_plus(location)}"
        try:
            r = self._get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        except requests.exceptions.RequestException as e:
            log.warning("jobteaser %r: %s", keyword, e)
            return []
        if r is None or r.status_code != 200:
            log.warning("jobteaser status %s", getattr(r, "status_code", None))
            return []
        return parse_jobteaser(r.text, location)[:limit]
