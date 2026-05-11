"""Gmail — search, read, label, archive, trash, send."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

__repld_usage__ = "gm = Gmail()"

_CREDS_PATH = os.path.expanduser("~/.config/repld/gmail_credentials.json")
_TOKEN_PATH = os.path.expanduser("~/.config/repld/google_token.json")
_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class Gmail:
    """Gmail — search, read, label, archive, trash, send.

    Uses OAuth2 with refresh token. Credentials stored at
    ~/.config/repld/gmail_credentials.json and google_token.json.
    """

    def __init__(self) -> None:
        self._token: dict = {}
        self._load_token()

    def _load_token(self) -> None:
        if os.path.exists(_TOKEN_PATH):
            with open(_TOKEN_PATH) as f:
                self._token = json.load(f)

    def _refresh(self) -> None:
        """Refresh the access token using the stored refresh token."""
        with open(_CREDS_PATH) as f:
            creds = json.load(f)["installed"]
        data = urllib.parse.urlencode({
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": self._token["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token",
                                     data=data, method="POST")
        with urllib.request.urlopen(req) as resp:
            new = json.loads(resp.read())
        self._token["access_token"] = new["access_token"]
        with open(_TOKEN_PATH, "w") as f:
            json.dump(self._token, f, indent=2)

    def _req(self, path: str, method: str = "GET", body: dict | None = None,
             raw: bool = False) -> dict | bytes:
        """Make an authenticated API request, auto-refreshing on 401."""
        url = f"{_API}/{path}" if not path.startswith("http") else path
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token['access_token']}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
                req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                             headers=headers, method=method)
            else:
                req = urllib.request.Request(url, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.read() if raw else json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._refresh()
                    continue
                raise

    # --- Read ---

    def search(self, query: str, limit: int = 20, headers: bool = True) -> list[dict]:
        """Search messages by Gmail query. -> [{id, thread_id, snippet, from, to, subject, date}]

        With headers=False, returns only {id, thread_id, snippet} (fast, no per-message fetch).
        """
        msgs = []
        page_token = None
        while len(msgs) < limit:
            params = {"q": query, "maxResults": min(limit - len(msgs), 100)}
            if page_token:
                params["pageToken"] = page_token
            data = self._req(f"messages?{urllib.parse.urlencode(params)}")
            for m in data.get("messages", []):
                if headers:
                    detail = self._req(f"messages/{m['id']}?format=metadata"
                                       f"&metadataHeaders=From&metadataHeaders=To"
                                       f"&metadataHeaders=Subject&metadataHeaders=Date")
                    msgs.append(self._parse_metadata(detail))
                else:
                    # Fast mode: just get snippet without per-message fetch
                    detail = self._req(f"messages/{m['id']}?format=minimal")
                    msgs.append({
                        "id": detail["id"],
                        "thread_id": detail.get("threadId", ""),
                        "snippet": detail.get("snippet", ""),
                        "date": "",
                        "from": "",
                        "subject": "",
                        "to": "",
                    })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return msgs

    def read(self, message_id: str) -> dict:
        """Read a full message. -> {id, thread_id, from, to, subject, date, body, labels}"""
        detail = self._req(f"messages/{message_id}?format=full")
        parsed = self._parse_metadata(detail)
        parsed["body"] = self._extract_body(detail.get("payload", {}))
        parsed["labels"] = detail.get("labelIds", [])
        return parsed

    def thread(self, thread_id: str) -> list[dict]:
        """Read all messages in a thread. -> [{id, from, subject, date, snippet}]"""
        data = self._req(f"threads/{thread_id}?format=metadata"
                         f"&metadataHeaders=From&metadataHeaders=Subject"
                         f"&metadataHeaders=Date")
        return [self._parse_metadata(m) for m in data.get("messages", [])]

    def labels(self) -> list[dict]:
        """List all labels. -> [{id, name, type}]"""
        data = self._req("labels")
        return [{"id": l["id"], "name": l["name"], "type": l.get("type", "")}
                for l in data.get("labels", [])]

    def inbox(self, limit: int = 20) -> list[dict]:
        """List inbox messages. -> [{id, thread_id, snippet, from, subject, date}]"""
        return self.search("in:inbox", limit=limit)

    def unread(self, limit: int = 20) -> list[dict]:
        """List unread messages. -> [{id, thread_id, snippet, from, subject, date}]"""
        return self.search("is:unread", limit=limit)

    # --- Write ---

    def archive(self, message_id: str) -> None:
        """Archive a message (remove INBOX label). -> None"""
        self._req(f"messages/{message_id}/modify",
                  method="POST", body={"removeLabelIds": ["INBOX"]})

    def trash(self, message_id: str) -> None:
        """Move a message to trash. -> None"""
        self._req(f"messages/{message_id}/trash", method="POST")

    def untrash(self, message_id: str) -> None:
        """Remove a message from trash. -> None"""
        self._req(f"messages/{message_id}/untrash", method="POST")

    def mark_read(self, message_id: str) -> None:
        """Mark a message as read. -> None"""
        self._req(f"messages/{message_id}/modify",
                  method="POST", body={"removeLabelIds": ["UNREAD"]})

    def mark_unread(self, message_id: str) -> None:
        """Mark a message as unread. -> None"""
        self._req(f"messages/{message_id}/modify",
                  method="POST", body={"addLabelIds": ["UNREAD"]})

    def label(self, message_id: str, label_ids: list[str]) -> None:
        """Add labels to a message. -> None"""
        self._req(f"messages/{message_id}/modify",
                  method="POST", body={"addLabelIds": label_ids})

    def unlabel(self, message_id: str, label_ids: list[str]) -> None:
        """Remove labels from a message. -> None"""
        self._req(f"messages/{message_id}/modify",
                  method="POST", body={"removeLabelIds": label_ids})

    def send(self, to: str, subject: str, body: str) -> dict:
        """Send an email. -> {id, thread_id}"""
        import base64
        raw = f"To: {to}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}"
        encoded = base64.urlsafe_b64encode(raw.encode()).decode()
        return self._req("messages/send", method="POST", body={"raw": encoded})

    # --- Bulk ---

    def archive_many(self, message_ids: list[str]) -> None:
        """Archive multiple messages. -> None"""
        self._req("messages/batchModify", method="POST",
                  body={"ids": message_ids, "removeLabelIds": ["INBOX"]})

    def trash_many(self, message_ids: list[str]) -> None:
        """Trash multiple messages. -> None"""
        for mid in message_ids:
            self.trash(mid)

    def mark_read_many(self, message_ids: list[str]) -> None:
        """Mark multiple messages as read. -> None"""
        self._req("messages/batchModify", method="POST",
                  body={"ids": message_ids, "removeLabelIds": ["UNREAD"]})

    # --- Helpers ---

    @staticmethod
    def _parse_metadata(msg: dict) -> dict:
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", msg).get("headers", [])}
        return {
            "id": msg["id"],
            "thread_id": msg.get("threadId", ""),
            "snippet": msg.get("snippet", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
        }

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Extract body from message payload. Prefers text/plain, falls back to HTML→text."""
        import base64

        def _find(node: dict, mime: str) -> str | None:
            if node.get("mimeType") == mime and node.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(node["body"]["data"]).decode("utf-8", errors="replace")
            for part in node.get("parts", []):
                found = _find(part, mime)
                if found:
                    return found
            return None

        # Prefer plaintext
        text = _find(payload, "text/plain")
        if text:
            return text

        # Fall back to HTML — strip tags for readable text
        html = _find(payload, "text/html")
        if html:
            import re
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.S)
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.S)
            html = re.sub(r'<[^>]+>', ' ', html)
            html = re.sub(r'&nbsp;', ' ', html)
            html = re.sub(r'&amp;', '&', html)
            html = re.sub(r'&#\d+;', '', html)
            html = re.sub(r'\s+', ' ', html).strip()
            return html

        return ""
