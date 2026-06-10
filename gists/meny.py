"""Meny.no — Norwegian grocery store. Search products, nutrition, prices, categories."""

__repld_usage__ = "meny = await Meny.connect()"

from urllib.parse import quote

import repld

_BASE = "https://platform-rest-prod.ngdata.no/api"
_DEFAULT_STORE = "7080001150488"
_DEFAULT_CHAIN = "1300"


class Meny:
    """Meny grocery API — search, nutrition, prices, allergens, categories."""

    def __init__(
        self, tab, *, store_id: str = _DEFAULT_STORE, chain_id: str = _DEFAULT_CHAIN
    ) -> None:
        self._tab = tab
        self._store = store_id
        self._chain = chain_id

    @classmethod
    async def connect(cls, store_id: str = _DEFAULT_STORE) -> "Meny":
        """Attach to meny.no tab or open one."""
        try:
            tab = await repld.browser.get("*meny.no*")
        except RuntimeError:
            tab = await repld.browser.open("https://meny.no")
            await tab.wait_for("role=main", timeout=10)
        await tab.pin("Meny — repld integration")
        return cls(tab, store_id=store_id)

    async def search(
        self, query: str, *, page: int = 1, page_size: int = 20
    ) -> list[dict]:
        """Search products by text. -> [{ean, title, vendor, price, kcal, protein, fat, carbs, allergens_contains, url, ...}]"""
        r = await self._tab.fetch(
            f"{_BASE}/episearch/{self._chain}/products"
            f"?search={quote(query)}&page={page}&page_size={page_size}"
            f"&suggest=true&types=products&store_id={self._store}"
            f"&popularity=true&full_response=true&showNotForSale=true",
            headers={"Content-Type": "application/json"},
        )
        eans = [h["contentId"] for h in r["body"]["hits"]["hits"]]
        if not eans:
            return []
        return await self.products(eans)

    async def products(self, eans: list[str]) -> list[dict]:
        """Fetch full product details by EAN codes. -> same shape as search()"""
        r = await self._tab.fetch(
            f"{_BASE}/products/{self._chain}/{self._store}"
            f"?page=1&page_size={len(eans)}&full_response=true"
            f"&fieldset=maximal&facets=Category,Allergen"
            f"&showNotForSale=true&product_ids={','.join(eans)}",
            headers={"Content-Type": "application/json"},
        )
        return [self._parse(h["_source"]) for h in r["body"]["hits"]["hits"]]

    async def browse(
        self,
        *,
        category_id: int | None = None,
        search: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> list[dict]:
        """Browse products by category and/or keyword. -> same shape as search()"""
        params = (
            f"?page={page}&page_size={page_size}&full_response=true"
            f"&fieldset=maximal&facets=Category,Allergen&showNotForSale=true"
        )
        if category_id:
            params += f"&category_id={category_id}"
        if search:
            params += f"&search={quote(search)}"
        r = await self._tab.fetch(
            f"{_BASE}/products/{self._chain}/{self._store}{params}",
            headers={"Content-Type": "application/json"},
        )
        return [self._parse(h["_source"]) for h in r["body"]["hits"]["hits"]]

    async def categories(self) -> list[dict]:
        """Get the full category tree."""
        r = await self._tab.fetch("https://meny.no/api/categories")
        return r["body"]

    async def suggest(self, query: str) -> dict:
        """Typeahead suggestions (products, recipes, articles, stores)."""
        r = await self._tab.fetch(
            f"{_BASE}/episearch/{self._chain}/autosuggest"
            f"?types=suggest,products,recipes,articles,faqs,stores"
            f"&search={quote(query)}&page_size=5&store_id={self._store}"
            f"&popularity=true&showNotForSale=false&version=1",
            headers={"Content-Type": "application/json"},
        )
        return r["body"]

    @staticmethod
    def _parse(src: dict) -> dict:
        nutr_raw = src.get("nutritionalContent", [])
        nutrition = {
            n["name"]: {
                "amount": n["amount"],
                "unit": n["unit"],
                "label": n["displayName"],
            }
            for n in nutr_raw
        }
        allergen_raw = src.get("allergens", [])
        contains = [a["displayName"] for a in allergen_raw if a["code"] != "FRI"]
        free_from = [a["displayName"] for a in allergen_raw if a["code"] == "FRI"]
        return {
            "ean": src.get("ean"),
            "title": src.get("title"),
            "subtitle": src.get("subtitle"),
            "vendor": src.get("vendor"),
            "category": src.get("categoryName"),
            "price": src.get("pricePerUnit"),
            "price_original": src.get("pricePerUnitOriginal"),
            "compare_price": src.get("comparePricePerUnit"),
            "compare_unit": src.get("compareUnit"),
            "is_offer": src.get("isOffer"),
            "weight_kg": src.get("weight"),
            "package_grams": src.get("measurementValue"),
            "package_size": src.get("packageSize"),
            "unit": src.get("unit"),
            "nutrition_per_100g": nutrition,
            "kcal": nutrition.get("energi_kcal", {}).get("amount"),
            "protein": nutrition.get("protein", {}).get("amount"),
            "fat": nutrition.get("fett_totalt", {}).get("amount"),
            "carbs": nutrition.get("karbohydrater", {}).get("amount"),
            "fiber": nutrition.get("kostfiber", {}).get("amount"),
            "sugar": nutrition.get("sukkerarter", {}).get("amount"),
            "salt": nutrition.get("salt", {}).get("amount"),
            "saturated_fat": nutrition.get("mettet_fett", {}).get("amount"),
            "allergens_contains": contains,
            "allergens_free_from": free_from,
            "organic": src.get("organic"),
            "country": src.get("productionCountry"),
            "storage": src.get("storage"),
            "url": f"https://meny.no{src['slugifiedUrl']}"
            if src.get("slugifiedUrl")
            else None,
            "image": f"https://bilder.ngdata.no/{src['imageGtin']}/meny/large.jpg"
            if src.get("imageGtin")
            else None,
        }
