"""Brønnøysundregistrene — Norwegian business registry. Companies, roles, search."""

import asyncio
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

__repld_usage__ = "b = Brreg(); results = await b.search(kommune='5001', nace='62')"

_BASE = "https://data.brreg.no/enhetsregisteret/api"


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{_BASE}{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def _parse_company(e: dict) -> dict:
    addr = e.get("forretningsadresse") or e.get("postadresse") or {}
    nace = e.get("naeringskode1") or {}
    return {
        "orgnr": e["organisasjonsnummer"],
        "name": e.get("navn", ""),
        "org_form": e.get("organisasjonsform", {}).get("beskrivelse", ""),
        "address": ", ".join(addr.get("adresse", [])),
        "postcode": addr.get("postnummer", ""),
        "city": addr.get("poststed", ""),
        "kommune": addr.get("kommunenummer", ""),
        "nace_code": nace.get("kode", ""),
        "nace_desc": nace.get("beskrivelse", ""),
        "employees": e.get("antallAnsatte", 0),
        "founded": e.get("stiftelsesdato", ""),
        "registered": e.get("registreringsdatoEnhetsregisteret", ""),
        "status": "active" if not e.get("konkurs") else "bankrupt",
        "website": e.get("hjemmeside", ""),
    }


def _parse_role(group_type: str, r: dict) -> dict:
    p = r.get("person", {})
    name = p.get("navn", {})
    return {
        "group": group_type,
        "role": r["type"]["beskrivelse"],
        "role_code": r["type"]["kode"],
        "name": f"{name.get('fornavn', '')} {name.get('etternavn', '')}".strip(),
        "birth_date": p.get("fodselsdato", ""),
        "active": not r.get("fratraadt", False),
    }


class Brreg:
    """Brønnøysundregistrene — Norwegian company registry. Free API, no auth."""

    async def search(
        self,
        *,
        kommune: str | None = None,
        nace: str | None = None,
        navn: str | None = None,
        size: int = 20,
        page: int = 0,
    ) -> dict:
        """Search companies. Filter by kommune (5001=Trondheim), NACE industry code, name."""
        params = {
            "kommunenummer": kommune,
            "naeringskode": nace,
            "navn": navn,
            "size": str(size),
            "page": str(page),
        }
        data = await asyncio.to_thread(_get, "/enheter", params)
        companies = [
            _parse_company(e) for e in data.get("_embedded", {}).get("enheter", [])
        ]
        total = data.get("page", {}).get("totalElements", 0)
        return {"total": total, "companies": companies}

    async def company(self, orgnr: str) -> dict:
        """Get full company record by org number."""
        return _parse_company(await asyncio.to_thread(_get, f"/enheter/{orgnr}"))

    async def roles(self, orgnr: str) -> list[dict]:
        """Get board members, CEO, auditor etc. for a company."""
        data = await asyncio.to_thread(_get, f"/enheter/{orgnr}/roller")
        results = []
        for group in data.get("rollegrupper", []):
            group_type = group["type"]["beskrivelse"]
            for r in group.get("roller", []):
                parsed = _parse_role(group_type, r)
                if parsed["name"]:  # skip empty (auditor firms etc)
                    results.append(parsed)
        return results

    async def sub_units(self, orgnr: str) -> list[dict]:
        """Get sub-units (underenheter) for a parent company."""
        data = await asyncio.to_thread(_get, f"/enheter/{orgnr}/underenheter")
        return [
            _parse_company(e) for e in data.get("_embedded", {}).get("underenheter", [])
        ]

    async def search_sub_units(
        self,
        *,
        kommune: str | None = None,
        nace: str | None = None,
        navn: str | None = None,
        size: int = 20,
    ) -> dict:
        """Search sub-units (branch offices, departments)."""
        params = {
            "kommunenummer": kommune,
            "naeringskode": nace,
            "navn": navn,
            "size": str(size),
        }
        data = await asyncio.to_thread(_get, "/underenheter", params)
        units = [
            _parse_company(e) for e in data.get("_embedded", {}).get("underenheter", [])
        ]
        total = data.get("page", {}).get("totalElements", 0)
        return {"total": total, "units": units}
