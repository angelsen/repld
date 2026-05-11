"""X (Twitter) — search, profiles, tweets, likes, retweets, bookmarks, followers, following."""

from __future__ import annotations

import repld

__repld_usage__ = "x = await X.connect()"

# GraphQL endpoint IDs — discovered from webpack bundle.
# These rotate on deploys; if a 404 appears, re-scan.
_ENDPOINTS = {
    "SearchTimeline": "XN_HccZ9SU-miQVvwTAlFQ",
    "UserByScreenName": "IGgvgiOx4QZndDHuD3x9TQ",
    "UserByRestId": "VQfQ9wwYdk6j_u2O4vt64Q",
    "UserTweets": "naBcZ4al-iTCFBYGOAMzBQ",
    "TweetDetail": "QrLp7AR-eMyamw8D1N9l6A",
    "TweetResultByRestId": "fHLDP3qFEjnTqhWBVvsREg",
    "CreateTweet": "c50A_puUoQGK_4SXseYz3A",
    "DeleteTweet": "nxpZCY2K-I6QoFHAHeojFQ",
    "FavoriteTweet": "lI07N6Otwv1PhnEgXILM7A",
    "UnfavoriteTweet": "ZYKSe-w7KEslx3JhSIk5LA",
    "CreateRetweet": "mbRO74GrOvSfRcJnlMapnQ",
    "DeleteRetweet": "ZyZigVsNiFO6v1dEks1eWg",
    "CreateBookmark": "aoDbu3RHznuiSkQ9aNM67Q",
    "DeleteBookmark": "Wlmlj2-xzyS1GN3a6cj-mQ",
    "Followers": "xOdl9jiaOqwHUm68qsq6Hg",
    "Following": "lQxnNSmlJkQHod0yzbVYDg",
    "FollowersYouKnow": "OBA-ChVl1ZPvozFotXP0ag",
    "UserTweetsAndReplies": "YhE6S_TtdhVxLtpokXrRaA",
    "HomeTimeline": "3tb-_5Lf7kdCZ1cFHmsEfg",
    "Bookmarks": "1nFKbANnLDDNT2nyLFZxtQ",
}

# Bearer token is a public app constant, same for all sessions.
_BEARER = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# Default features sent with timeline/tweet queries.
_TIMELINE_FEATURES = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_USER_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
}


class X:
    """X (Twitter) internal API — search, profiles, tweets, likes, retweets, bookmarks.

    Auth: uses the browser session (cookies) + a per-request transaction ID
    generated from X.com's webpack bundle. All calls go through tab.js()
    which runs fetch() in the page context.
    """

    def __init__(self, tab) -> None:
        self._tab = tab

    @classmethod
    async def connect(cls) -> "X":
        """Find or open X.com and return a ready instance."""
        try:
            tab = await repld.browser.get("*://x.com/*")
        except RuntimeError:
            tab = await repld.browser.open("https://x.com")
            await tab.wait_for("role=main", timeout=10)
        await tab.pin("X (Twitter) — repld integration")
        # Check login state — ct0 cookie is the CSRF token, only set when authenticated
        cookies = await tab.js("document.cookie")
        if "ct0" not in (cookies or ""):
            raise RuntimeError(
                "Not logged in to X — log in at x.com first, then retry connect()"
            )
        # Bootstrap webpack require + find the transaction ID module.
        # Module IDs shift on every X.com deploy, so we discover by signature.
        ok = await tab.js("""
(function() {
    // 1. Extract webpack require if not already present
    if (!window.__webpackRequire) {
        const chunks = window.webpackChunk_twitter_responsive_web;
        if (!chunks || !chunks.length) return false;
        chunks.push([['__repld__'], {}, function(r) { window.__webpackRequire = r; }]);
        chunks.pop();
    }
    const req = window.__webpackRequire;
    if (!req) return false;

    // 2. Find the transaction ID module by signature — exports jJ (async function with 'jf.x.com')
    if (!window.__repld_txmod) {
        for (const id of Object.keys(req.m)) {
            const src = req.m[id].toString();
            if (src.includes('rweb_client_transaction_id_enabled') && src.includes('x-client-transaction-id')) {
                try {
                    const mod = req(parseInt(id) || id);
                    for (const k of Object.keys(mod)) {
                        if (typeof mod[k] === 'function' && mod[k].toString().includes('jf.x.com')) {
                            window.__repld_txmod = mod[k];
                            break;
                        }
                    }
                    if (window.__repld_txmod) break;
                } catch(e) {}
            }
        }
    }
    return !!window.__repld_txmod;
})()
""")
        if not ok:
            raise RuntimeError(
                "Failed to bootstrap X.com internals — is the page fully loaded?"
            )
        return cls(tab)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _gql(
        self,
        operation: str,
        variables: dict,
        *,
        method: str = "GET",
        features: dict | None = None,
    ) -> dict:
        """Execute a GraphQL operation. Returns parsed response body."""
        import json

        qid = _ENDPOINTS.get(operation)
        if qid is None:
            raise ValueError(f"Unknown operation: {operation}")

        path = f"/i/api/graphql/{qid}/{operation}"
        feat = features or _TIMELINE_FEATURES
        variables_json = json.dumps(variables)
        features_json = json.dumps(feat)

        if method == "GET":
            url_expr = (
                f"'https://x.com' + path"
                f" + '?variables=' + encodeURIComponent({json.dumps(variables_json)})"
                f" + '&features=' + encodeURIComponent({json.dumps(features_json)})"
            )
            fetch_opts = "{ headers: hdrs }"
        else:
            body_json = json.dumps(
                {"variables": variables, "features": feat, "queryId": qid}
            )
            url_expr = "'https://x.com' + path"
            fetch_opts = (
                f"{{ method: 'POST', headers: hdrs, body: {json.dumps(body_json)} }}"
            )

        js = f"""
(async function() {{
  const path = {json.dumps(path)};
  const txId = await window.__repld_txmod('x.com', path, {json.dumps(method)});
  const csrf = document.cookie.match(/ct0=([^;]+)/)?.[1];
  const hdrs = {{
    'authorization': {json.dumps(_BEARER)},
    'x-csrf-token': csrf,
    'x-twitter-auth-type': 'OAuth2Session',
    'x-twitter-active-user': 'yes',
    'x-twitter-client-language': 'en',
    'x-client-transaction-id': txId,
    'content-type': 'application/json',
  }};
  const r = await fetch({url_expr}, {fetch_opts});
  if (!r.ok) return {{ __error: true, status: r.status, body: await r.text() }};
  return await r.json();
}})()
"""
        result = await self._tab.js(js, await_promise=True)
        if isinstance(result, dict) and result.get("__error"):
            raise RuntimeError(
                f"{operation} -> {result['status']}: {result.get('body', '')[:200]}"
            )
        return result

    @staticmethod
    def _tweet_from_result(tr: dict) -> dict:
        """Extract a flat tweet dict from a tweet result node."""
        if tr.get("__typename") == "TweetWithVisibilityResults":
            tr = tr.get("tweet", tr)
        legacy = tr.get("legacy", {})
        user = tr.get("core", {}).get("user_results", {}).get("result", {})
        user_core = user.get("core", {})
        return {
            "id": tr.get("rest_id"),
            "text": legacy.get("full_text", ""),
            "created_at": legacy.get("created_at", ""),
            "likes": legacy.get("favorite_count", 0),
            "retweets": legacy.get("retweet_count", 0),
            "replies": legacy.get("reply_count", 0),
            "views": int(tr.get("views", {}).get("count", 0) or 0),
            "screen_name": user_core.get("screen_name", ""),
            "name": user_core.get("name", ""),
            "user_id": user.get("rest_id", ""),
            "verified": user.get("is_blue_verified", False),
            "bookmarked": legacy.get("bookmarked", False),
            "liked": legacy.get("favorited", False),
            "retweeted": legacy.get("retweeted", False),
        }

    @staticmethod
    def _parse_tweet(entry: dict) -> dict | None:
        """Extract a flat tweet dict from a timeline entry."""
        content = entry.get("content", {})
        item = content.get("itemContent", {})
        tr = item.get("tweet_results", {}).get("result", {})
        if not tr or not tr.get("rest_id"):
            return None
        return X._tweet_from_result(tr)

    @staticmethod
    def _parse_user(result: dict) -> dict:
        """Extract a flat user dict from a GraphQL user result."""
        core = result.get("core", {})
        legacy = result.get("legacy", {})
        return {
            "id": result.get("rest_id", ""),
            "screen_name": core.get("screen_name", ""),
            "name": core.get("name", ""),
            "bio": legacy.get("description", ""),
            "followers": legacy.get("followers_count", 0),
            "following": legacy.get("friends_count", 0),
            "tweets": legacy.get("statuses_count", 0),
            "likes": legacy.get("favourites_count", 0),
            "verified": result.get("is_blue_verified", False),
            "created_at": core.get("created_at", ""),
            "profile_image": legacy.get("profile_image_url_https", ""),
            "url": legacy.get("url", ""),
        }

    @staticmethod
    def _timeline_tweets(data: dict, path: list[str]) -> list[dict]:
        """Walk a nested dict path to find timeline entries, parse tweets."""
        node = data
        for key in path:
            node = node.get(key, {})
        instructions = node.get("instructions", [])
        tweets: list[dict] = []
        for inst in instructions:
            for entry in inst.get("entries", []):
                eid = entry.get("entryId", "")
                content = entry.get("content", {})
                typename = content.get("__typename", "")
                if eid.startswith("tweet-"):
                    t = X._parse_tweet(entry)
                    if t:
                        tweets.append(t)
                elif typename == "TimelineTimelineModule":
                    for item in content.get("items", []):
                        ic = item.get("item", {}).get("itemContent", {})
                        tr = ic.get("tweet_results", {}).get("result", {})
                        if tr.get("rest_id"):
                            tweets.append(X._tweet_from_result(tr))
        return tweets

    @staticmethod
    def _timeline_users(data: dict, path: list[str]) -> list[dict]:
        """Walk a nested dict path to find timeline user entries, parse users."""
        node = data
        for key in path:
            node = node.get(key, {})
        instructions = node.get("instructions", [])
        users: list[dict] = []
        for inst in instructions:
            for entry in inst.get("entries", []):
                content = entry.get("content", {})
                ic = content.get("itemContent", {})
                if ic.get("__typename") == "TimelineUser":
                    ur = ic.get("user_results", {}).get("result", {})
                    if ur.get("rest_id"):
                        users.append(X._parse_user(ur))
        return users

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def search(
        self, query: str, *, count: int = 20, product: str = "Top"
    ) -> list[dict]:
        """Search tweets. product: Top, Latest, People, Photos, Videos. -> [{id, text, screen_name, likes, retweets, views, ...}]"""
        data = await self._gql(
            "SearchTimeline",
            {
                "rawQuery": query,
                "count": count,
                "querySource": "typed_query",
                "product": product,
            },
        )
        return self._timeline_tweets(
            data, ["data", "search_by_raw_query", "search_timeline", "timeline"]
        )

    async def user(self, screen_name: str) -> dict:
        """Get user profile by @handle. -> {id, screen_name, name, bio, followers, following, tweets, verified}"""
        data = await self._gql(
            "UserByScreenName",
            {
                "screen_name": screen_name,
            },
            features=_USER_FEATURES,
        )
        result = data.get("data", {}).get("user", {}).get("result", {})
        if not result.get("rest_id"):
            raise RuntimeError(f"User not found: @{screen_name}")
        return self._parse_user(result)

    async def user_tweets(self, user_id: str, *, count: int = 20) -> list[dict]:
        """Get recent tweets by user ID. -> [{id, text, screen_name, likes, retweets, views, ...}]"""
        data = await self._gql(
            "UserTweets",
            {
                "userId": user_id,
                "count": count,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
            },
        )
        return self._timeline_tweets(
            data, ["data", "user", "result", "timeline", "timeline"]
        )

    async def tweet(self, tweet_id: str) -> dict:
        """Get a single tweet by ID. -> {id, text, screen_name, likes, retweets, views, ...}"""
        data = await self._gql(
            "TweetResultByRestId",
            {
                "tweetId": tweet_id,
                "withCommunity": True,
                "includePromotedContent": False,
                "withVoice": False,
            },
        )
        tr = data.get("data", {}).get("tweetResult", {}).get("result", {})
        if not tr.get("rest_id"):
            raise RuntimeError(f"Tweet not found: {tweet_id}")
        return self._tweet_from_result(tr)

    async def home(self, *, count: int = 20) -> list[dict]:
        """Get home timeline. -> [{id, text, screen_name, likes, retweets, views, ...}]"""
        data = await self._gql(
            "HomeTimeline",
            {
                "count": count,
                "includePromotedContent": False,
                "latestControlAvailable": True,
            },
        )
        return self._timeline_tweets(data, ["data", "home", "home_timeline_urt"])

    async def bookmarks(self, *, count: int = 20) -> list[dict]:
        """Get bookmarked tweets. -> [{id, text, screen_name, likes, retweets, views, ...}]"""
        data = await self._gql(
            "Bookmarks", {"count": count, "includePromotedContent": False}
        )
        return self._timeline_tweets(data, ["data", "bookmark_timeline_v2", "timeline"])

    async def followers(self, user_id: str, *, count: int = 50) -> list[dict]:
        """Get followers of a user by user ID. -> [{id, screen_name, name, bio, followers, verified, ...}]"""
        data = await self._gql(
            "Followers",
            {"userId": user_id, "count": count, "includePromotedContent": False},
        )
        return self._timeline_users(
            data, ["data", "user", "result", "timeline", "timeline"]
        )

    async def following(self, user_id: str, *, count: int = 50) -> list[dict]:
        """Get accounts a user follows by user ID. -> [{id, screen_name, name, bio, followers, verified, ...}]"""
        data = await self._gql(
            "Following",
            {"userId": user_id, "count": count, "includePromotedContent": False},
        )
        return self._timeline_users(
            data, ["data", "user", "result", "timeline", "timeline"]
        )

    async def followers_you_know(self, user_id: str, *, count: int = 50) -> list[dict]:
        """Get mutual followers (people you follow who also follow this user). -> [{id, screen_name, name, bio, followers, verified, ...}]"""
        data = await self._gql(
            "FollowersYouKnow",
            {"userId": user_id, "count": count, "includePromotedContent": False},
        )
        return self._timeline_users(
            data, ["data", "user", "result", "timeline", "timeline"]
        )

    async def user_replies(self, user_id: str, *, count: int = 20) -> list[dict]:
        """Get tweets and replies by user ID (shows who they engage with). -> [{id, text, screen_name, likes, ...}]"""
        data = await self._gql(
            "UserTweetsAndReplies",
            {
                "userId": user_id,
                "count": count,
                "includePromotedContent": False,
                "withCommunity": True,
                "withVoice": True,
            },
        )
        return self._timeline_tweets(
            data, ["data", "user", "result", "timeline", "timeline"]
        )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def like(self, tweet_id: str) -> dict:
        """Like a tweet."""
        return await self._gql("FavoriteTweet", {"tweet_id": tweet_id}, method="POST")

    async def unlike(self, tweet_id: str) -> dict:
        """Unlike a tweet."""
        return await self._gql("UnfavoriteTweet", {"tweet_id": tweet_id}, method="POST")

    async def retweet(self, tweet_id: str) -> dict:
        """Retweet a tweet."""
        return await self._gql(
            "CreateRetweet",
            {"tweet_id": tweet_id, "dark_request": False},
            method="POST",
        )

    async def unretweet(self, tweet_id: str) -> dict:
        """Undo a retweet."""
        return await self._gql(
            "DeleteRetweet",
            {"source_tweet_id": tweet_id, "dark_request": False},
            method="POST",
        )

    async def bookmark(self, tweet_id: str) -> dict:
        """Bookmark a tweet."""
        return await self._gql("CreateBookmark", {"tweet_id": tweet_id}, method="POST")

    async def unbookmark(self, tweet_id: str) -> dict:
        """Remove a bookmark."""
        return await self._gql("DeleteBookmark", {"tweet_id": tweet_id}, method="POST")

    async def post(self, text: str, *, reply_to: str | None = None) -> dict:
        """Post a tweet. Optionally reply to a tweet_id."""
        preview = text[:80] + ("…" if len(text) > 80 else "")
        ok = await self._tab.confirm(f'Post tweet: "{preview}"?')
        if not ok:
            raise RuntimeError("Post cancelled by user")
        variables: dict = {
            "tweet_text": text,
            "dark_request": False,
            "media": {"media_entities": [], "possibly_sensitive": False},
            "semantic_annotation_ids": [],
        }
        if reply_to:
            variables["reply"] = {
                "in_reply_to_tweet_id": reply_to,
                "exclude_reply_user_ids": [],
            }
        data = await self._gql("CreateTweet", variables, method="POST")
        tr = (
            data.get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result", {})
        )
        return {
            "id": tr.get("rest_id", ""),
            "text": tr.get("legacy", {}).get("full_text", ""),
        }

    async def delete(self, tweet_id: str) -> dict:
        """Delete a tweet."""
        ok = await self._tab.confirm(f"Delete tweet {tweet_id}?")
        if not ok:
            raise RuntimeError("Delete cancelled by user")
        return await self._gql(
            "DeleteTweet", {"tweet_id": tweet_id, "dark_request": False}, method="POST"
        )
