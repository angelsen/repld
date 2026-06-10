"""LinkedIn — profiles, companies, search, employees."""

import json
from urllib.parse import quote, urlencode

import repld

__repld_usage__ = "li = await LI.connect()"

_PROFILE_DECORATION = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-109"
)


class LI:
    """LinkedIn internal API — profiles, companies, search, employees."""

    def __init__(self, tab, csrf: str) -> None:
        self._tab = tab
        self._csrf = csrf

    @classmethod
    async def connect(cls) -> "LI":
        """Find or open LinkedIn and return a ready instance."""
        try:
            tab = await repld.browser.get("*linkedin.com*")
        except RuntimeError:
            tab = await repld.browser.open("https://www.linkedin.com")
            await tab.wait_for_idle(timeout=15)
        await tab.pin("LinkedIn — repld integration")
        csrf = await tab.js(
            "document.cookie.match(/JSESSIONID=\"?([^;\"]+)/)?.[1] || ''"
        )
        if not csrf:
            raise RuntimeError("No JSESSIONID — are you logged in?")
        return cls(tab, csrf)

    # ------------------------------------------------------------------
    # Voyager API (JSON)
    # ------------------------------------------------------------------

    async def _refresh_csrf(self):
        """Re-read JSESSIONID cookie (survives tab navigation / session refresh)."""
        csrf = await self._tab.js(
            "document.cookie.match(/JSESSIONID=\"?([^;\"]+)/)?.[1] || ''"
        )
        if csrf:
            self._csrf = csrf

    async def _voyager(
        self, path: str, params: dict | None = None, _retried: bool = False
    ) -> dict:
        """Call a voyager REST API endpoint. Auto-refreshes CSRF on 403."""
        url = f"https://www.linkedin.com/voyager/api/{path}"
        if params:
            url += "?" + urlencode(params)
        r = await self._tab.fetch(
            url,
            headers={
                "csrf-token": self._csrf,
                "accept": "application/vnd.linkedin.normalized+json+2.1",
            },
        )
        if r["status"] == 403 and not _retried:
            await self._refresh_csrf()
            return await self._voyager(path, params, _retried=True)
        if r["status"] != 200:
            raise RuntimeError(f"LinkedIn API {r['status']}: {path}")
        return json.loads(r["body"]) if isinstance(r["body"], str) else r["body"]

    async def me(self) -> dict:
        """Get own profile summary. -> {id, first_name, last_name, headline, public_id, member_id, urn}"""
        data = await self._voyager("me")
        mini = {}
        for item in data.get("included", []):
            if item.get("firstName"):
                mini = item
                break
        return {
            "id": data.get("data", {}).get("plainId"),
            "first_name": mini.get("firstName", ""),
            "last_name": mini.get("lastName", ""),
            "headline": mini.get("occupation", ""),
            "public_id": mini.get("publicIdentifier", ""),
            "member_id": mini.get("dashEntityUrn", "").split(":")[-1] or None,
            "urn": mini.get("dashEntityUrn", ""),
        }

    async def profile(self, identifier: str) -> dict:
        """Get full profile by public_id or member_id ('ACoAA...'). -> {first_name, headline, positions, education, skills, ...}"""
        try:
            data = await self._voyager(
                "identity/dash/profiles",
                {
                    "q": "memberIdentity",
                    "memberIdentity": identifier,
                    "decorationId": _PROFILE_DECORATION,
                },
            )
            return _parse_profile(data)
        except RuntimeError:
            if identifier.startswith("ACoAA"):
                raise  # member_id failed — no fallback
            # public_id 403 for non-connected profiles — search for member_id and retry
            results = await self.search(identifier.replace("-", " "), count=5)
            for r in results:
                if r.get("public_id") == identifier:
                    return await self.profile(r["member_id"])
            raise

    async def company(self, universal_name: str) -> dict:
        """Get company by URL slug (e.g. 'attensi'). -> {name, company_id, staff_count, hq_city, website, ...}"""
        data = await self._voyager(
            "organization/companies",
            {"q": "universalName", "universalName": universal_name},
        )
        return _parse_company(data)

    # ------------------------------------------------------------------
    # RSC search (HTML fetch + parse)
    # ------------------------------------------------------------------

    async def search(
        self,
        keywords: str = "",
        *,
        count: int = 10,
        company: str | None = None,
        geo: str | None = None,
        network: str | None = None,
    ) -> list[dict]:
        """Search people. -> [{name, headline, location, member_id, public_id?, url?}]

        Filters: company (ID or name), geo (ID), network (F/S/O).
        Paginates automatically up to `count`.
        """
        company_id = await self._resolve_company_id(company) if company else None
        people: list[dict] = []
        page = 1
        while len(people) < count:
            url = self._build_search_url(keywords, page, company_id, geo, network)
            r = await self._tab.fetch(url, headers={"csrf-token": self._csrf})
            if r["status"] != 200:
                break
            batch = _parse_search_rsc(r["body"])
            if not batch:
                break
            people.extend(batch)
            page += 1
        return people[:count]

    async def employees(
        self, company: str, keywords: str = "", *, count: int = 10
    ) -> list[dict]:
        """List employees at a company. Shorthand for search(keywords, company=...)."""
        return await self.search(keywords, company=company, count=count)

    async def _resolve_company_id(self, company: str) -> str:
        """Resolve a company name/slug to its numeric ID."""
        if company.isdigit():
            return company
        info = await self.company(company)
        urn = info.get("urn", "")
        cid = urn.split(":")[-1]
        if not cid.isdigit():
            raise RuntimeError(f"Could not resolve company ID for '{company}'")
        return cid

    @staticmethod
    def _build_search_url(
        keywords: str,
        page: int,
        company_id: str | None,
        geo: str | None,
        network: str | None,
    ) -> str:
        base = "https://www.linkedin.com/search/results/people/?"
        params = {"origin": "FACETED_SEARCH"}
        if keywords:
            params["keywords"] = keywords
        if company_id:
            params["currentCompany"] = f'["{company_id}"]'
        if geo:
            params["geoUrn"] = f'["{geo}"]'
        if network:
            params["network"] = f'["{network}"]'
        if page > 1:
            params["page"] = str(page)
        return base + urlencode(params, quote_via=quote)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


_SKIP_BOLD = frozenset(
    {
        "Besøk nettsiden min",
        "Visit my website",
        "Om dette medlemmet",
        "Send profil i en melding",
        "Lagre til PDF",
        "Rapporter / blokker",
        "Knytt kontakt",
        "Venter på svar",
        "Kontaktinformasjon",
    }
)


def _parse_search_rsc(html: str) -> list[dict]:
    """Extract people from LinkedIn search page via RSC rehydration."""
    from rsc import find_snippet_ids, parse_rehydration, parse_tree, walk_text

    lines = parse_rehydration(html)
    if not lines:
        return []

    for data in lines.values():
        member_ids = find_snippet_ids(data)
        if not member_ids:
            continue
        tree = parse_tree(data)
        if tree is None:
            continue
        texts = walk_text(tree)
        people: list[dict] = []
        mid_idx = 0
        i = 0
        while i < len(texts) and mid_idx < len(member_ids):
            t = texts[i]
            if t["weight"] == "bold" and t["text"] not in _SKIP_BOLD:
                is_private = t["text"] == "LinkedIn-medlem"
                name = None if is_private else t["text"]
                slug = t.get("slug")
                headline = _next_text(texts, i + 1)
                location = _next_text(texts, i + 2)
                if headline == "--":
                    headline = ""
                person: dict = {
                    "name": name,
                    "headline": headline,
                    "location": location,
                    "member_id": member_ids[mid_idx],
                }
                if slug:
                    person["public_id"] = slug
                    person["url"] = f"https://www.linkedin.com/in/{slug}/"
                people.append(person)
                mid_idx += 1
                i += 3
            else:
                i += 1
        return people
    return []


def _next_text(texts: list[dict], idx: int) -> str:
    """Get text at idx if it's not bold (avoids bleeding into next card)."""
    if idx < len(texts) and texts[idx]["weight"] != "bold":
        return texts[idx]["text"]
    return ""


def _parse_profile(data: dict) -> dict:
    """Extract structured profile from voyager FullProfileWithEntities response."""
    included = data.get("included", [])

    profile: dict = {}
    for item in included:
        if "Profile" in item.get("$type", "") and item.get("firstName"):
            profile = {
                "first_name": item.get("firstName", ""),
                "last_name": item.get("lastName", ""),
                "headline": item.get("headline", ""),
                "summary": item.get("summary", ""),
                "public_id": item.get("publicIdentifier", ""),
                "member_id": (item.get("entityUrn") or "").split(":")[-1] or None,
                "urn": item.get("entityUrn", ""),
            }
            break

    positions = []
    for item in included:
        if "Position" in item.get("$type", "") and item.get("title"):
            pos = {
                "title": item.get("title", ""),
                "company": item.get("companyName", ""),
                "location": item.get("locationName", ""),
            }
            start = item.get("dateRange", {}).get("start")
            end = item.get("dateRange", {}).get("end")
            if start:
                pos["start"] = f"{start.get('year', '')}-{start.get('month', 1):02d}"
            if end:
                pos["end"] = f"{end.get('year', '')}-{end.get('month', 1):02d}"
            else:
                pos["current"] = True
            positions.append(pos)
    # Current roles first, then by start date descending
    positions.sort(
        key=lambda p: (p.get("current", False), p.get("start", "0")), reverse=True
    )
    profile["positions"] = positions

    education = []
    for item in included:
        if "Education" in item.get("$type", "") and item.get("schoolName"):
            education.append(
                {
                    "school": item.get("schoolName", ""),
                    "degree": item.get("degreeName", ""),
                    "field": item.get("fieldOfStudy", ""),
                }
            )
    profile["education"] = education

    skills = []
    for item in included:
        if "Skill" in item.get("$type", "") and item.get("name"):
            skills.append(item["name"])
    profile["skills"] = skills

    return profile


def _parse_company(data: dict) -> dict:
    """Extract company info from voyager organization/companies response."""
    for item in data.get("included", []):
        if item.get("name") and item.get("staffCount"):
            hq = item.get("headquarter") or {}
            urn = item.get("entityUrn", "")
            return {
                "name": item.get("name", ""),
                "universal_name": item.get("universalName", ""),
                "company_id": urn.split(":")[-1] if urn else None,
                "tagline": item.get("tagline", ""),
                "description": item.get("description", ""),
                "staff_count": item.get("staffCount"),
                "hq_city": hq.get("city", ""),
                "hq_country": hq.get("country", ""),
                "website": item.get("url", ""),
                "founded": item.get("foundedOn", ""),
                "specialities": item.get("specialities", []),
                "urn": urn,
            }
    return {}
