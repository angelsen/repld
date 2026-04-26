"""Proff.no — Norwegian company directory. Revenue, employees, roles, shareholders, financials."""

import json
import re

__repld_usage__ = "p = await Proff.connect()"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>'
)


class Proff:
    """Proff.no company directory — roles, shareholders, revenue, financials."""

    def __init__(self, tab) -> None:
        self._tab = tab

    @classmethod
    async def connect(cls) -> "Proff":
        """Find or open proff.no and return a ready instance."""
        from __main__ import browser

        try:
            tab = await browser.get("*proff.no*")
        except RuntimeError:
            tab = await browser.open("https://www.proff.no")
        return cls(tab)

    async def search(self, query: str) -> list[dict]:
        """Search companies by name or orgnr. Returns list of {name, url, city, industry}."""
        r = await self._tab.fetch(
            f"https://www.proff.no/bransjes%C3%B8k?q={_quote(query)}"
        )
        if r["status"] != 200:
            return []
        results = []
        for m in re.finditer(
            r'href="(/selskap/([^/]+)/([^/]+)/([^/]+)/[^"]+)"', r["body"]
        ):
            url = f"https://www.proff.no{m.group(1)}"
            name = m.group(2).replace("-", " ").title()
            city = m.group(3).replace("-", " ").title()
            industry = m.group(4).replace("-", " ")
            if url not in [r["url"] for r in results]:
                results.append(
                    {"name": name, "url": url, "city": city, "industry": industry}
                )
        return results

    async def company(self, url_or_orgnr: str) -> dict:
        """Get full company data. Pass a proff.no URL or an orgnr."""
        if url_or_orgnr.startswith("http"):
            url = url_or_orgnr
        else:
            hits = await self.search(url_or_orgnr)
            if not hits:
                raise ValueError(f"No company found for: {url_or_orgnr}")
            url = hits[0]["url"]

        r = await self._tab.fetch(url)
        if r["status"] != 200:
            raise RuntimeError(f"Failed to fetch {url}: HTTP {r['status']}")

        m = _NEXT_DATA_RE.search(r["body"])
        if not m:
            raise RuntimeError("No __NEXT_DATA__ found on page")

        raw = json.loads(m.group(1))["props"]["pageProps"]["company"]
        return _parse_company(raw)

    async def roles(self, url_or_orgnr: str) -> list[dict]:
        """Get board members, CEO, auditor for a company."""
        co = await self.company(url_or_orgnr)
        return co.get("roles", [])

    async def shareholders(self, url_or_orgnr: str) -> list[dict]:
        """Get shareholders for a company."""
        co = await self.company(url_or_orgnr)
        return co.get("shareholders", [])

    async def financials(self, url_or_orgnr: str) -> list[dict]:
        """Get financial accounts history for a company."""
        co = await self.company(url_or_orgnr)
        return co.get("accounts", [])


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def _parse_company(raw: dict) -> dict:
    """Extract the useful fields from proff.no's __NEXT_DATA__ company blob."""
    roles = []
    for group in raw.get("roles", {}).get("roleGroups", []):
        for r in group.get("roles", []):
            roles.append(
                {
                    "group": group.get("name", ""),
                    "role": r.get("role", ""),
                    "name": r.get("name", ""),
                    "type": r.get("type", ""),
                    "birth_date": r.get("birthDate", ""),
                    "person_id": r.get("id", ""),
                }
            )

    shareholders = [
        {
            "name": s.get("name", ""),
            "share_pct": s.get("share", ""),
            "num_shares": s.get("numberOfShares", 0),
            "company_id": s.get("companyId"),
        }
        for s in raw.get("shareholders", [])
    ]

    accounts = []
    for a in raw.get("companyAccounts", []):
        # accounts is a list of {code, amount} pairs — convert to dict
        codes = {}
        for item in a.get("accounts", []):
            if isinstance(item, dict):
                codes[item.get("code", "")] = item.get("amount")
        accounts.append(
            {
                "year": a.get("year"),
                "period": a.get("period"),
                "revenue": codes.get("SDI"),  # sum driftsinntekter
                "profit": codes.get("AARS"),  # årsresultat
                "ebitda": codes.get("EBITDA"),
                "total_assets": codes.get("SIA"),  # sum eiendeler
                "equity": codes.get("SEK"),  # sum egenkapital
                "short_term_debt": codes.get("SKG"),  # sum kortsiktig gjeld
                "long_term_debt": codes.get("LG"),  # langsiktig gjeld
                "employees": codes.get("OPAV"),  # antall ansatte
            }
        )

    addr = raw.get("legalVisitorAddress") or raw.get("visitorAddress") or {}
    postal = raw.get("legalPostalAddress") or raw.get("postalAddress") or {}

    return {
        "name": raw.get("name", ""),
        "legal_name": raw.get("legalName", ""),
        "orgnr": raw.get("orgnr", ""),
        "company_type": (raw.get("companyType") or {}).get("name", ""),
        "status": (raw.get("status") or {}).get("status", ""),
        "employees": raw.get("numberOfEmployees"),
        "revenue": raw.get("revenue"),
        "profit": raw.get("profit"),
        "share_capital": raw.get("shareCapital"),
        "founded": raw.get("foundationDate", ""),
        "nace": raw.get("naceIndustries", []),
        "address": _format_addr(addr),
        "postal_address": _format_addr(postal),
        "homepage": raw.get("homePage", ""),
        "email": raw.get("email", ""),
        "phone": raw.get("phone", ""),
        "description": raw.get("description", ""),
        "ceo": (raw.get("roles", {}).get("manager") or {}).get("name", ""),
        "chairman": (raw.get("roles", {}).get("chairman") or {}).get("name", ""),
        "roles": roles,
        "shareholders": shareholders,
        "accounts": accounts,
    }


def _format_addr(addr: dict) -> str:
    parts = []
    if addr.get("addressLine"):
        parts.append(addr["addressLine"])
    if addr.get("zipCode") or addr.get("area"):
        parts.append(f"{addr.get('zipCode', '')} {addr.get('area', '')}".strip())
    return ", ".join(parts)
