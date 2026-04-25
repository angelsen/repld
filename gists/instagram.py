"""Instagram — profiles, DMs, search, likes, comments, follows."""

from __future__ import annotations

__repld_usage__ = "ig = await IG.connect()"


class IG:
    """Instagram — profiles, DMs, search, likes, comments, follows.

    Usage:
        ig = await IG.connect()
        await ig.inbox()
    """

    _APP_ID = "936619743392459"

    def __init__(self, tab) -> None:
        self._tab = tab

    @classmethod
    async def connect(cls) -> "IG":
        """Attach to Instagram tab and return ready instance."""
        from __main__ import browser

        tab = await browser.get("*://www.instagram.com/*")
        await tab.pin("Instagram — profiles, DMs, search, likes, comments, follows")
        return cls(tab)

    @staticmethod
    def _parse(body) -> dict:
        """Ensure body is a parsed dict (tab.fetch may return raw JSON string)."""
        if isinstance(body, dict):
            return body
        import json

        return json.loads(body)

    async def _csrf(self) -> str:
        """Extract csrftoken from cookies."""
        token = await self._tab.js("document.cookie.match(/csrftoken=([^;]+)/)?.[1]")
        if not token:
            raise RuntimeError("No csrftoken cookie — not logged in?")
        return token

    async def _rest(self, path: str, *, params: dict | None = None) -> dict:
        """v1 REST GET. Returns parsed JSON body."""
        headers = {
            "x-ig-app-id": self._APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "x-csrftoken": await self._csrf(),
        }
        url = f"https://www.instagram.com/api/v1/{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url += f"?{qs}"
        r = await self._tab.fetch(url, headers=headers)
        if r["status"] != 200:
            raise RuntimeError(f"GET {path} -> {r['status']}")
        return self._parse(r["body"])

    async def _rest_post(self, path: str, body: dict | None = None) -> dict:
        """v1 REST POST. Returns parsed JSON body."""
        headers = {
            "x-ig-app-id": self._APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "x-csrftoken": await self._csrf(),
        }
        r = await self._tab.fetch(
            f"https://www.instagram.com/api/v1/{path}",
            method="POST",
            headers=headers,
            body=body or {},
        )
        if r["status"] not in (200, 201):
            raise RuntimeError(f"POST {path} -> {r['status']}")
        return self._parse(r["body"])

    async def _gql(self, doc_id: str, friendly_name: str, variables: dict) -> dict:
        """GraphQL mutation via /api/graphql."""
        import json
        from urllib.parse import quote

        csrf = await self._csrf()
        fb_dtsg = await self._tab.js(
            "window.require('DTSGInitialData')?.token || "
            "document.querySelector('[name=\"fb_dtsg\"]')?.value"
        )
        if not fb_dtsg:
            raise RuntimeError("fb_dtsg not found — page not fully loaded?")
        body_str = "&".join(
            f"{k}={quote(str(v), safe='')}"
            for k, v in {
                "doc_id": doc_id,
                "fb_api_req_friendly_name": friendly_name,
                "fb_api_caller_class": "RelayModern",
                "variables": json.dumps(variables),
                "fb_dtsg": fb_dtsg,
            }.items()
        )
        r = await self._tab.fetch(
            "https://www.instagram.com/api/graphql",
            method="POST",
            headers={
                "x-ig-app-id": self._APP_ID,
                "x-csrftoken": csrf,
                "content-type": "application/x-www-form-urlencoded",
            },
            body=body_str,
        )
        if r["status"] != 200:
            raise RuntimeError(f"GraphQL {friendly_name} -> {r['status']}")
        return self._parse(r["body"])

    # -- Profiles --

    async def profile_by_username(self, username: str) -> dict:
        """Get profile by username. Returns user object with pk, bio, counts."""
        return await self._rest(
            "users/web_profile_info/", params={"username": username}
        )

    async def profile(self, user_pk: str) -> dict:
        """Get profile by numeric PK."""
        return await self._rest(f"users/{user_pk}/info/")

    async def search(self, query: str) -> dict:
        """Search users, hashtags, places."""

        return await self._gql(
            "26367005406296257",
            "PolarisSearchBoxRefetchableQuery",
            {
                "data": {
                    "context": "blended",
                    "include_reel": "true",
                    "query": query,
                    "rank_token": "",
                    "search_surface": "web_top_search",
                },
                "hasQuery": True,
            },
        )

    # -- Social graph --

    async def followers(
        self, user_pk: str, *, count: int = 20, max_id: str | None = None
    ) -> dict:
        """Paginated follower list."""
        return await self._rest(
            f"friendships/{user_pk}/followers/",
            params={"count": str(count), "max_id": max_id},
        )

    async def following(
        self, user_pk: str, *, count: int = 20, max_id: str | None = None
    ) -> dict:
        """Paginated following list."""
        return await self._rest(
            f"friendships/{user_pk}/following/",
            params={"count": str(count), "max_id": max_id},
        )

    # -- DMs --

    async def inbox(self, *, limit: int = 20) -> dict:
        """DM thread list. Returns inbox.threads with thread_v2_id."""
        return await self._rest("direct_v2/inbox/", params={"limit": str(limit)})

    async def thread(self, thread_id: str, *, limit: int = 20) -> dict:
        """Read DM thread by long thread_id (REST)."""
        return await self._rest(
            f"direct_v2/threads/{thread_id}/", params={"limit": str(limit)}
        )

    async def thread_messages(self, thread_v2_id: str) -> dict:
        """Read DM thread messages by thread_v2_id (GraphQL, 20 per page)."""
        return await self._gql(
            "34625130820435173",
            "IGDThreadDetailMainViewContainerQuery",
            {
                "min_uq_seq_id": None,
                "thread_fbid": thread_v2_id,
                "__relay_internal__pv__IGDEnableOffMsysPinnedMessagesQErelayprovider": False,
                "__relay_internal__pv__IGDInitialMessagePageCountrelayprovider": 20,
            },
        )

    async def send_message(
        self, thread_v2_id: str, text: str, *, reply_to: str | None = None
    ) -> dict:
        """Send a DM. Pass reply_to=message_id to reply to a specific message."""
        import time

        preview = text[:80] + ("…" if len(text) > 80 else "")
        if not await self._tab.confirm(f'Send DM: "{preview}"?'):
            raise RuntimeError("Send cancelled by user")
        return await self._gql(
            "27313801781553196",
            "IGDirectTextSendMutation",
            {
                "ig_thread_igid": thread_v2_id,
                "offline_threading_id": str(int(time.time() * 1000)),
                "recipient_igids": None,
                "replied_to_client_context": None,
                "replied_to_item_id": None,
                "reply_to_message_id": reply_to,
                "text": {"sensitive_string_value": text},
                "mentions": [],
                "mentioned_user_ids": [],
                "commands": None,
            },
        )

    # -- Posts --

    async def like(self, media_pk: str) -> dict:
        """Like a post."""
        return await self._rest_post(f"web/likes/{media_pk}/like/")

    async def unlike(self, media_pk: str) -> dict:
        """Unlike a post."""
        return await self._rest_post(f"web/likes/{media_pk}/unlike/")

    async def comments(self, media_pk: str) -> dict:
        """List comments on a post."""
        return await self._rest(
            f"media/{media_pk}/comments/", params={"can_support_threading": "true"}
        )

    async def add_comment(self, media_pk: str, text: str) -> dict:
        """Add a comment on a post."""
        preview = text[:80] + ("…" if len(text) > 80 else "")
        if not await self._tab.confirm(f'Post comment: "{preview}"?'):
            raise RuntimeError("Comment cancelled by user")
        return await self._rest_post(
            f"web/comments/{media_pk}/add/", {"comment_text": text}
        )

    async def delete_comment(self, media_pk: str, comment_pk: str) -> dict:
        """Delete a comment."""
        if not await self._tab.confirm(f"Delete comment {comment_pk}?"):
            raise RuntimeError("Delete cancelled by user")
        return await self._rest_post(f"web/comments/{media_pk}/delete/{comment_pk}/")

    # -- Notifications --

    async def notifications(self) -> dict:
        """Activity notifications (likes, comments, follows)."""
        return await self._rest("news/inbox/")

    # -- Recipes (wire with @every or defer) --

    _seen_threads: set[str]

    async def watch_inbox(self, on_new=None) -> None:
        """Poll inbox, call on_new for unread incoming threads. Use with @every(30).

        Skips threads where the last message is from you (own sends).
        Tracks seen thread+item pairs to avoid duplicate notifications.
        Default on_new is notify().
        """
        if on_new is None:
            from __main__ import notify

            on_new = notify
        if not hasattr(self, "_seen_threads"):
            self._seen_threads = set()
        data = await self.inbox()
        viewer_pk = data.get("viewer", {}).get("pk")
        threads = data["inbox"]["threads"]
        for t in threads:
            last = t.get("last_permanent_item", {})
            # Skip own sends
            if viewer_pk and last.get("user_id") == viewer_pk:
                continue
            key = f"{t['thread_v2_id']}:{last.get('item_id', '')}"
            if key in self._seen_threads:
                continue
            self._seen_threads.add(key)
            user = t["users"][0]["username"] if t.get("users") else "unknown"
            text = (
                last.get("text")
                or last.get("action_log", {}).get("description")
                or last.get("item_type", "")
            )
            on_new(
                f"{user}: {text}",
                kind="ig_dm",
                thread_v2_id=t["thread_v2_id"],
                username=user,
            )
