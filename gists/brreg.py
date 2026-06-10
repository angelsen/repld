"""Brønnøysundregistrene — Norwegian business registry. Companies, roles, search."""

import httpx

__repld_deps__ = ["httpx>=0.27"]
__repld_usage__ = "b = Brreg(); results = await b.search(kommune='5001', nace='62')"

_BASE = "https://data.brreg.no/enhetsregisteret/api"
_client: httpx.AsyncClient | None = None


async def _get(path: str, params: dict | None = None) -> dict:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=_BASE, timeout=15)
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    resp = await _client.get(path, params=clean, headers={"Accept": "application/json"})
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()


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
    ent = r.get("enhet", {})  # org-backed roles: auditor, accountant
    return {
        "group": group_type,
        "role": r["type"]["beskrivelse"],
        "role_code": r["type"]["kode"],
        "name": f"{name.get('fornavn', '')} {name.get('etternavn', '')}".strip()
        or " ".join(ent.get("navn", [])),
        "orgnr": ent.get("organisasjonsnummer", ""),
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
        """Search companies by kommune (5001=Trondheim), NACE code, name. -> {total, companies: [{orgnr, name, city, nace_code, employees, ...}]}"""
        params = {
            "kommunenummer": kommune,
            "naeringskode": nace,
            "navn": navn,
            "size": str(size),
            "page": str(page),
        }
        data = await _get("/enheter", params)
        companies = [
            _parse_company(e) for e in data.get("_embedded", {}).get("enheter", [])
        ]
        total = data.get("page", {}).get("totalElements", 0)
        return {"total": total, "companies": companies}

    async def company(self, orgnr: str) -> dict:
        """Get one company by org number. -> {orgnr, name, org_form, address, city, nace_code, employees, founded, status, website, ...}"""
        return _parse_company(await _get(f"/enheter/{orgnr}"))

    async def roles(self, orgnr: str) -> list[dict]:
        """Get board, CEO, auditor, accountant for a company. -> [{group, role, role_code, name, orgnr, birth_date, active}]"""
        data = await _get(f"/enheter/{orgnr}/roller")
        results = []
        for group in data.get("rollegrupper", []):
            group_type = group["type"]["beskrivelse"]
            for r in group.get("roller", []):
                parsed = _parse_role(group_type, r)
                if parsed["name"]:
                    results.append(parsed)
        return results

    async def sub_units(self, orgnr: str) -> list[dict]:
        """Get sub-units (underenheter) for a parent company. -> [{orgnr, name, city, nace_code, ...}]"""
        data = await _get(f"/enheter/{orgnr}/underenheter")
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
        """Search sub-units (branch offices, departments). -> {total, units: [{orgnr, name, city, nace_code, ...}]}"""
        params = {
            "kommunenummer": kommune,
            "naeringskode": nace,
            "navn": navn,
            "size": str(size),
        }
        data = await _get("/underenheter", params)
        units = [
            _parse_company(e) for e in data.get("_embedded", {}).get("underenheter", [])
        ]
        total = data.get("page", {}).get("totalElements", 0)
        return {"total": total, "units": units}
