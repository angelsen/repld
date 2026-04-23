"""Shopify Search & Discovery app — synonyms, boosts, filters, recommendations, settings."""

from __future__ import annotations

import re


class SD:
    """Shopify Search & Discovery app — synonyms, boosts, filters, recommendations, settings.

    Two-tab model: navigates on the admin tab, fetches from the S&D iframe.

    Usage:
        sd = await SD.connect()
        await sd.filters()
    """

    __repld_usage__ = "sd = await SD.connect()"

    def __init__(self, admin_tab, iframe_tab) -> None:
        self._admin = admin_tab
        self._tab = iframe_tab
        self._store = self._extract_store(admin_tab.url)

    @staticmethod
    def _extract_store(url: str) -> str:
        """Extract store handle from admin URL."""
        m = re.search(r"/store/([^/]+)", url)
        if not m:
            raise RuntimeError(f"Cannot extract store from URL: {url}")
        return m.group(1)

    @classmethod
    async def connect(cls) -> "SD":
        """Find or open S&D in admin, attach to iframe, return ready instance."""
        from __main__ import browser

        try:
            admin = await browser.get("*admin.shopify*search-and-discovery*")
        except RuntimeError:
            admin = await browser.open(
                "https://admin.shopify.com/store/mym-shop-7ai85jfe/apps/search-and-discovery"
            )
        iframe = await browser.get("*search-and-discovery.shopifyapps*", timeout=10)
        return cls(admin, iframe)

    async def _navigate(self, path: str) -> None:
        """Navigate admin tab to an S&D sub-page, wait for new iframe."""
        from __main__ import browser

        url = f"https://admin.shopify.com/store/{self._store}/apps/search-and-discovery/{path}"
        await self._admin.navigate(url)
        self._tab = await browser.get(
            "*search-and-discovery.shopifyapps*", timeout=10, fresh=True
        )

    async def _get(self, path: str, route: str) -> dict:
        """GET a Remix loader route. Returns parsed JSON body."""
        r = await self._tab.fetch(
            f"/{path}?_data={route}",
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        if r["status"] != 200:
            raise RuntimeError(f"GET /{path} -> {r['status']}: {r['body']}")
        return r["body"]

    async def _post(self, path: str, route: str, body: dict) -> dict:
        """POST a Remix action route (mutation). Returns parsed JSON body."""
        r = await self._tab.fetch(
            f"/{path}?_data={route}",
            method="POST",
            body=body,
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        if r["status"] not in (200, 201, 204):
            raise RuntimeError(f"POST /{path} -> {r['status']}: {r['body']}")
        return r["body"]

    # -- Synonyms --

    async def synonyms(self) -> dict:
        """List all synonym groups."""
        return await self._get("search/synonyms", "routes%2Fsearch.synonyms._index")

    async def synonym(self, id: str) -> dict:
        """Get a single synonym group by ID."""
        return await self._get(
            f"search/synonyms/{id}", "routes%2Fsearch.synonyms.%24id"
        )

    async def create_synonym(self, terms: list[str], title: str | None = None) -> dict:
        """Create a synonym group. Title defaults to first term."""
        import json

        return await self._post(
            "search/synonyms/new",
            "routes%2Fsearch.synonyms.%24id",
            {
                "action": "create",
                "payload": {
                    "type": "synonym_group",
                    "metafields": [
                        {"key": "title", "value": title or terms[0]},
                        {"key": "synonyms", "value": json.dumps(terms)},
                    ],
                },
            },
        )

    async def update_synonym(
        self, id: str, terms: list[str], title: str | None = None
    ) -> dict:
        """Update a synonym group's terms and/or title. Accepts numeric ID or full GID."""
        import json

        gid = f"gid://shopify/Metaobject/{id}" if not id.startswith("gid://") else id
        numeric = gid.split("/")[-1]
        return await self._post(
            f"search/synonyms/{numeric}",
            "routes%2Fsearch.synonyms.%24id",
            {
                "action": "update",
                "payload": {
                    "id": gid,
                    "metafields": [
                        {"key": "title", "value": title or terms[0]},
                        {"key": "synonyms", "value": json.dumps(terms)},
                    ],
                },
            },
        )

    async def delete_synonym(self, id: str) -> dict:
        """Delete a synonym group. Accepts numeric ID or full GID."""
        gid = f"gid://shopify/Metaobject/{id}" if not id.startswith("gid://") else id
        numeric = gid.split("/")[-1]
        return await self._post(
            f"search/synonyms/{numeric}",
            "routes%2Fsearch.synonyms.%24id",
            {"action": "delete", "payload": {"id": gid}},
        )

    async def delete_synonyms(self, ids: list[str]) -> dict:
        """Bulk delete synonym groups."""
        return await self._post(
            "search/synonyms",
            "routes%2Fsearch.synonyms._index",
            {"action": "delete", "payload": {"ids": ids}},
        )

    # -- Product Boosts --

    async def boosts(self) -> dict:
        """List product boost rules."""
        return await self._get(
            "search/product-boosts", "routes%2Fsearch.product-boosts._index"
        )

    async def boost(self, id: str) -> dict:
        """Get a single boost rule by ID."""
        return await self._get(
            f"search/product-boosts/{id}", "routes%2Fsearch.product-boosts.%24id"
        )

    async def upsert_boost(self, product_id: str, metafields: list[dict]) -> dict:
        """Create or update a product boost rule."""
        return await self._post(
            f"search/product-boosts/{product_id}",
            "routes%2Fsearch.product-boosts.%24id",
            {"action": "upsert", "payload": {"metafields": metafields}},
        )

    async def delete_boost(self, product_id: str, metafields: list[dict]) -> dict:
        """Delete a product boost rule."""
        return await self._post(
            f"search/product-boosts/{product_id}",
            "routes%2Fsearch.product-boosts.%24id",
            {"action": "delete", "payload": {"metafields": metafields}},
        )

    # -- Filters --

    async def filters(self) -> dict:
        """List all filter settings."""
        return await self._get("filters", "routes%2Ffilters._index")

    async def filter(self, id: str) -> dict:
        """Get a single filter by ID."""
        return await self._get(f"filters/{id}", "routes%2Ffilters.%24id")

    async def create_filter(
        self,
        name: str,
        label: str | None = None,
        category: str = "VARIANT",
        filter_type: str = "LIST",
        sort_order: str = "AUTOMATIC",
    ) -> dict:
        """Add a filter. Navigates to filters/new, then creates.

        Common filters:
            create_filter("availability", "Tilgjengelighet")
            create_filter("price", "Pris", filter_type="PRICE_RANGE")
            create_filter("vendor", "Merke", category="PRODUCT")
            create_filter("product_type", "Produkttype", category="PRODUCT")
            create_filter("tag", "Tagger", category="PRODUCT")
        """
        return await self._post(
            "filters/new",
            "routes%2Ffilters.%24id",
            {
                "action": "create",
                "payload": {
                    "input": {
                        "label": label or name.replace("_", " ").title(),
                        "category": category,
                        "filterType": filter_type,
                        "name": name,
                        "sortOrder": sort_order,
                        "displaySettings": None,
                    }
                },
            },
        )

    async def delete_filter(self, id: str) -> dict:
        """Delete a filter by ID (full GID or numeric)."""
        numeric = id.split("/")[-1] if "/" in id else id
        return await self._post(
            f"filters/{numeric}",
            "routes%2Ffilters.%24id",
            {"action": "delete", "payload": {"id": id}},
        )

    async def reorder_filters(self, filter_ids: list[str]) -> dict:
        """Reorder filters. Pass filter IDs in desired order."""
        return await self._post(
            "filters",
            "routes%2Ffilters._index",
            {
                "action": "reorder",
                "payload": {"filters": [{"id": fid} for fid in filter_ids]},
            },
        )

    # -- Recommendations --

    async def recommendations(self) -> dict:
        """List product recommendation configs."""
        return await self._get(
            "product-recommendations",
            "routes%2Fproduct-recommendations._index",
        )

    async def recommendation(self, id: str) -> dict:
        """Get a single recommendation config."""
        return await self._get(
            f"product-recommendations/{id}",
            "routes%2Fproduct-recommendations.%24id",
        )

    async def save_recommendation(
        self, product_id: str, metafields: list[dict]
    ) -> dict:
        """Save product recommendations (related/complementary)."""
        return await self._post(
            f"product-recommendations/{product_id}",
            "routes%2Fproduct-recommendations.%24id",
            {"action": "save", "payload": {"metafields": metafields}},
        )

    async def delete_recommendation(
        self, product_id: str, metafield_ids: list[str]
    ) -> dict:
        """Delete product recommendations."""
        return await self._post(
            f"product-recommendations/{product_id}",
            "routes%2Fproduct-recommendations.%24id",
            {
                "action": "delete",
                "payload": {"productId": product_id, "metafieldIds": metafield_ids},
            },
        )

    # -- Settings --

    async def settings(self) -> dict:
        """Get search & discovery settings."""
        return await self._get("settings", "routes%2Fsettings._index")

    async def update_settings(self, **config) -> dict:
        """Update search & discovery settings.

        Accepts: searchConfiguration, filtersConfiguration, recommendationConfiguration.
        """
        return await self._post(
            "settings",
            "routes%2Fsettings._index",
            {"action": "update", "payload": config},
        )

    # -- Search overview --

    async def overview(self) -> dict:
        """Get search overview (boost + synonym counts)."""
        return await self._get("search", "routes%2Fsearch._index")
