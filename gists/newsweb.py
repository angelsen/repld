"""Oslo Børs NewsWeb — aksjemeldinger, insider trades, financial reports."""

import asyncio
import json
import urllib.request
from urllib.parse import urlencode

__repld_usage__ = "nw = Newsweb(); msgs = await nw.search(title='Aker')"

_BASE = "https://api3.oslo.oslobors.no/v1/newsreader"


def _post(path: str, params: dict | None = None) -> dict:
    url = f"{_BASE}/{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        url, method="POST", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


class Newsweb:
    """Oslo Børs NewsWeb — public stock exchange announcements. No auth needed."""

    async def search(
        self,
        *,
        title: str | None = None,
        issuer: int | None = None,
        category: int | None = None,
        market: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict]:
        """Search filings (1005=inside info, 1102=managers' tx, 1006=major holdings). -> [{id, title, issuer, published, category, ...}]"""
        data = await asyncio.to_thread(
            _post,
            "list",
            {
                "messageTitle": title or "",
                "issuer": str(issuer) if issuer else "",
                "category": str(category) if category else "",
                "market": market or "",
                "fromDate": from_date or "",
                "toDate": to_date or "",
            },
        )
        return [_parse_message(m) for m in data.get("data", {}).get("messages", [])]

    async def issuers(self, active_only: bool = True) -> list[dict]:
        """List issuers on Oslo Børs. -> [{id, symbol, name}]"""
        data = await asyncio.to_thread(_post, "issuers")
        issuers = data.get("data", {}).get("issuers", [])
        if active_only:
            issuers = [i for i in issuers if i.get("isActive")]
        return [
            {
                "id": i.get("issuerId"),
                "symbol": i.get("symbol", ""),
                "name": i.get("name", ""),
            }
            for i in issuers
        ]

    async def categories(self) -> list[dict]:
        """List filing categories (inside info, managers' tx, etc). -> [{id, name_en, name_no}]"""
        data = await asyncio.to_thread(_post, "categories")
        return [
            {
                "id": c.get("id"),
                "name_en": c.get("category_en", ""),
                "name_no": c.get("category_no", ""),
            }
            for c in data.get("data", {}).get("categories", [])
        ]

    async def markets(self) -> list[dict]:
        """List markets (XOSL=Oslo Børs, XOAX=Euronext Expand, etc)."""
        data = await asyncio.to_thread(_post, "markets")
        return data.get("data", {}).get("markets", [])

    async def find_issuer(self, name: str) -> list[dict]:
        """Find issuer by name or symbol substring. -> [{id, symbol, name}]"""
        issuers = await self.issuers()
        q = name.lower()
        return [
            i for i in issuers if q in i["name"].lower() or q in i["symbol"].lower()
        ]


def _parse_message(m: dict) -> dict:
    cats = m.get("category", [])
    return {
        "id": m.get("messageId"),
        "title": m.get("title", ""),
        "issuer": m.get("issuerName", ""),
        "issuer_sign": m.get("issuerSign", ""),
        "issuer_id": m.get("issuerId"),
        "published": m.get("publishedTime", ""),
        "category": cats[0].get("category_en", "") if cats else "",
        "category_id": cats[0].get("id") if cats else None,
        "markets": m.get("markets", []),
        "attachments": m.get("numbAttachments", 0),
    }
