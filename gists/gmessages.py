"""Google Messages — conversations, contacts, send/receive SMS & RCS."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from datetime import datetime

__repld_usage__ = "gm = await GMessages.connect()"

_ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
_DB_PATH = "/tmp/gmessages.db"


class GMessages:
    """Google Messages — web interface + ADB message history.

    Web path: internal Angular services for conversations, send, archive.
    ADB path: full SMS + RCS history dump into local SQLite for search.
    """

    def __init__(self, tab) -> None:
        self._tab = tab
        self._db: sqlite3.Connection | None = None

    @classmethod
    async def connect(cls) -> "GMessages":
        """Attach to Google Messages tab and capture the internal service."""
        from __main__ import browser
        try:
            tab = await browser.get("*messages.google.com*")
        except RuntimeError:
            tab = await browser.open("https://messages.google.com/web/conversations")
            await tab.wait_for("role=listbox", timeout=15)
        await tab.pin("Google Messages — repld integration")
        await cls._ensure_service(tab)
        return cls(tab)

    @staticmethod
    async def _ensure_service(tab) -> None:
        """One-shot hook to capture the SH messaging service instance."""
        has = await tab.js("!!window._shInstance")
        if has:
            return
        await tab.js("""
(() => {
    const mw = window.default_mw;

    // Capture SH instance (messaging service) on first sendMessage
    const SHClass = mw.SH;
    window._origSHSend = SHClass.prototype.sendMessage;
    const origSH = SHClass.prototype.sendMessage;
    SHClass.prototype.sendMessage = function(...args) {
        window._shInstance = this;
        window._msgService = this.Ib;
        SHClass.prototype.sendMessage = origSH;
        return origSH.apply(this, args);
    };

    // Capture jI instance (data service) on first XE call (message load)
    const jIClass = mw.jI;
    const origXE = jIClass.prototype.XE;
    window._origJIXE = origXE;
    jIClass.prototype.XE = function(...args) {
        window._jIInstance = this;
        jIClass.prototype.XE = origXE;
        return origXE.apply(this, args);
    };
})()
""")

    async def _get_state(self) -> dict:
        """Subscribe to the NgRx store and return a snapshot."""
        return await self._tab.js("""
(() => {
    const sh = window._shInstance;
    if (!sh) throw new Error('Service not captured yet — send a message first to initialize');
    let state = null;
    sh.select(x => x).subscribe(val => { state = val; }).unsubscribe();
    return state;
})()
""")

    async def conversations(self) -> list[dict]:
        """List loaded conversations. -> [{id, name, phone, last_message, timestamp, e2ee}]"""
        return await self._tab.js("""
(() => {
    const sh = window._shInstance;
    if (!sh) throw new Error('Service not captured — send a message first');
    let state = null;
    sh.select(x => x).subscribe(val => { state = val; }).unsubscribe();

    const convMap = state.conversations.conversations;
    const convs = [];

    function walk(node, depth) {
        if (!node || depth > 10) return;
        if (typeof node !== 'object') return;
        if (node.name !== undefined && node.participants !== undefined) {
            const other = node.participants.filter(p => !p.isSelf);
            convs.push({
                id: node.id,
                name: node.name,
                phone: other[0]?.Ab?.id || '',
                last_message: node.kd?.text || '',
                last_sender: node.kd?.vj || '',
                timestamp: node.kd?.Md || node.Md,
                e2ee: !!node.hasBeenE2ee,
                type: node.type,
                muted: !!node.isMuted,
                _raw: node
            });
            return;
        }
        for (const key of Object.keys(node)) {
            const child = node[key];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }
    }
    walk(convMap.map.root, 0);

    convs.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
    return convs.map(({_raw, ...rest}) => rest);
})()
""")

    async def contacts(self) -> list[dict]:
        """List synced contacts. Lazy-loaded on first call. -> [{name, phone, id}]"""
        return await self._tab.js("""
(async () => {
    const sh = window._shInstance;
    if (!sh) throw new Error('Service not captured — send a message first');
    let state = null;
    sh.select(x => x).subscribe(val => { state = val; }).unsubscribe();

    // Contacts are lazy — trigger load by navigating to new conversation view
    if (state.contacts.contacts.map.size === 0) {
        const btn = Array.from(document.querySelectorAll('a'))
            .find(a => a.textContent.includes('Start chat'));
        if (btn) {
            btn.click();
            await new Promise(r => setTimeout(r, 2000));
            // Go back
            const back = document.querySelector('button[aria-label="Back"]') ||
                Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('←'));
            if (back) back.click();
            await new Promise(r => setTimeout(r, 500));
            // Re-read state
            sh.select(x => x).subscribe(val => { state = val; }).unsubscribe();
        }
    }

    const contactMap = state.contacts.contacts;
    const contacts = [];

    function walk(node, depth) {
        if (!node || depth > 10) return;
        if (typeof node !== 'object') return;
        if (node.key !== undefined && node.value !== undefined) {
            const v = node.value;
            contacts.push({
                name: v.displayName || '',
                phone: v.Wf?.[0]?.ad || v.Wf?.[0]?.destination || '',
                id: v.id || node.key
            });
            return;
        }
        for (const k of Object.keys(node)) {
            const child = node[k];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }
    }
    walk(contactMap.map.root, 0);

    contacts.sort((a, b) => a.name.localeCompare(b.name));
    return contacts;
})()
""")

    async def search(self, query: str) -> list[dict]:
        """Search conversations and contacts by name or phone. -> [{name, phone, id, source}]"""
        convs = await self.conversations()
        results = []
        q = query.lower()
        for c in convs:
            if q in c["name"].lower() or q in c.get("phone", "").replace(" ", ""):
                results.append({**c, "source": "conversation"})
        return results

    async def send(self, conversation_id: str, text: str) -> dict:
        """Send a text message to a conversation. -> {ok, conversationId}"""
        ok = await self._tab.confirm(f"Send to {conversation_id}: \"{text[:60]}\"?")
        if not ok:
            raise RuntimeError("Cancelled by user")
        return await self._tab.js(f"""
(async () => {{
    const sh = window._shInstance;
    if (!sh) throw new Error('Service not captured — send a message first');
    let state = null;
    sh.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();

    const convId = {repr(conversation_id)};
    const text = {repr(text)};

    const convMap = state.conversations.conversations;
    let targetConv = null;

    function walk(node, depth) {{
        if (!node || depth > 10 || targetConv) return;
        if (typeof node !== 'object') return;
        if (node.id === convId) {{ targetConv = node; return; }}
        for (const key of Object.keys(node)) {{
            const child = node[key];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }}
    }}
    walk(convMap.map.root, 0);

    if (!targetConv) throw new Error('Conversation not found: ' + convId);

    const tmpId = 'tmp_' + Math.floor(Math.random() * 999999999999);
    const message = {{
        conversationId: convId,
        id: tmpId,
        Sf: 1,
        Ed: true,
        Yf: false,
        Hb: {{
            0: {{
                order: 9007199254740991,
                Yb: "0",
                type: "text",
                text: text
            }}
        }},
        le: targetConv.Sl || "2",
        status: 1,
        timestampMs: Date.now(),
        type: targetConv.type || 1,
        yf: tmpId
    }};

    await window._origSHSend.call(sh, {{message, Ka: targetConv}});
    return {{ok: true, conversationId: convId}};
}})()
""")

    async def messages(self, conversation_id: str) -> list[dict]:
        """Read messages for a conversation from the internal store. -> [{id, from_me, text, timestamp}]

        Navigates to the conversation (via internal router) if messages aren't
        loaded yet. Reads from the decrypted in-memory store — works for both
        SMS and E2EE conversations.
        """
        import asyncio

        # Check if messages are already loaded
        loaded = await self._tab.js(f"""
(() => {{
    const sel = window._shInstance || window._jIInstance;
    if (!sel) return false;
    let state = null;
    sel.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();
    return !!state.Qc?.Ds?.[{repr(conversation_id)}]?.Qc;
}})()
""")
        if not loaded:
            # Navigate to load messages — find and click the conversation link
            await self._tab.js(f"""
(() => {{
    const sel = window._shInstance || window._jIInstance;
    let state = null;
    sel.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();
    let targetName = null;
    function walkConvs(node, depth) {{
        if (!node || depth > 10 || targetName) return;
        if (typeof node !== 'object') return;
        if (node.id === {repr(conversation_id)}) {{ targetName = node.name; return; }}
        for (const key of Object.keys(node)) {{
            const child = node[key];
            if (child && typeof child === 'object') walkConvs(child, depth + 1);
        }}
    }}
    walkConvs(state.conversations.conversations.map.root, 0);
    if (!targetName) throw new Error('Conversation not found: ' + {repr(conversation_id)});
    const links = document.querySelectorAll('a[data-e2e-conversation]');
    for (const link of links) {{
        const name = link.querySelector('[data-e2e-conversation-name]')?.textContent?.trim();
        if (name === targetName) {{ link.click(); return; }}
    }}
    throw new Error('Conversation link not in DOM — try loading more conversations');
}})()
""")
            # Wait for messages to load into the store
            for _ in range(10):
                await asyncio.sleep(1)
                ready = await self._tab.js(f"""
(() => {{
    const sel = window._shInstance || window._jIInstance;
    let state = null;
    sel.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();
    return !!state.Qc?.Ds?.[{repr(conversation_id)}]?.Qc;
}})()
""")
                if ready:
                    break

        return await self._tab.js(f"""
(() => {{
    const sel = window._shInstance || window._jIInstance;
    if (!sel) throw new Error('Service not captured');
    let state = null;
    sel.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();

    const convData = state.Qc?.Ds?.[{repr(conversation_id)}];
    if (!convData?.Qc) throw new Error('Messages not loaded for ' + {repr(conversation_id)});

    const qcMap = convData.Qc;
    const msgs = [];
    function walk(node, depth) {{
        if (!node || depth > 10) return;
        if (typeof node !== 'object') return;
        if (node.id !== undefined && node.conversationId !== undefined) {{
            msgs.push(node);
            return;
        }}
        for (const key of Object.keys(node)) {{
            const child = node[key];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }}
    }}
    if (qcMap.map) walk(qcMap.map.root, 0);
    else walk(qcMap, 0);

    msgs.sort((a, b) => (a.timestampMs || 0) - (b.timestampMs || 0));
    return msgs.map(m => ({{
        id: m.id,
        from_me: !!m.Ed,
        text: m.Hb?.[0]?.text || '',
        timestamp: m.timestampMs,
        type: m.Hb?.[0]?.type || 'text'
    }}));
}})()
""")

    async def _conv_action(self, conversation_id: str, method: str) -> None:
        """Fire a conversation action (archive/delete) via the internal service."""
        # Fire-and-forget: the service call succeeds but the promise hangs
        # waiting for phone-web sync. We fire it and poll the store instead.
        await self._tab.js(f"""
(() => {{
    const convId = {repr(conversation_id)};
    const svc = window._msgService;
    if (!svc) throw new Error('Service not captured');
    const sh = window._shInstance;
    let state = null;
    sh.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();

    let conv = null;
    function walk(node, depth) {{
        if (!node || depth > 10 || conv) return;
        if (typeof node !== 'object') return;
        if (node.id === convId) {{ conv = node; return; }}
        for (const key of Object.keys(node)) {{
            const child = node[key];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }}
    }}
    walk(state.conversations.conversations.map.root, 0);
    if (!conv) throw new Error('Conversation not found: ' + convId);
    svc.{method}(conv);  // fire, don't await
}})()
""")

    async def archive(self, conversation_id: str) -> None:
        """Archive a conversation. -> None"""
        await self._conv_action(conversation_id, "archiveConversation")

    async def delete(self, conversation_id: str) -> None:
        """Move a conversation to bin. -> None"""
        await self._conv_action(conversation_id, "deleteConversation")

    async def permanent_delete(self, conversation_id: str) -> None:
        """Permanently delete a conversation (bypasses bin). -> None"""
        await self._tab.js(f"""
(() => {{
    const ji = window._jIInstance;
    if (!ji) throw new Error('jI not captured');
    // Temporarily disable bin flag so deleteConversation uses action 1 (permanent)
    const origAb = ji.flags.ab.bind(ji.flags);
    const ljFlag = ji.flags.Va.LJ;
    ji.flags.ab = function(flag) {{
        if (flag === ljFlag) return false;
        return origAb(flag);
    }};

    const sh = window._shInstance;
    let state = null;
    sh.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();

    let conv = null;
    function walk(node, depth) {{
        if (!node || depth > 10 || conv) return;
        if (typeof node !== 'object') return;
        if (node.id === {repr(conversation_id)}) {{ conv = node; return; }}
        for (const key of Object.keys(node)) {{
            const child = node[key];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }}
    }}
    walk(state.conversations.conversations.map.root, 0);
    if (!conv) throw new Error('Conversation not found: ' + {repr(conversation_id)});

    window._msgService.deleteConversation(conv);  // fire, don't await

    // Restore flag after a tick
    setTimeout(() => {{ ji.flags.ab = origAb; }}, 100);
}})()
""")

    async def find_or_create(self, name_or_phone: str) -> dict | None:
        """Find a conversation by name or phone number. -> {id, name, phone, ...} or None"""
        convs = await self.conversations()
        q = name_or_phone.lower().replace(" ", "")
        for c in convs:
            if q in c["name"].lower() or q in c.get("phone", "").replace(" ", ""):
                return c
        return None

    async def send_to(self, name_or_phone: str, text: str) -> dict:
        """Find conversation by name/phone and send a message. -> {ok, conversationId}"""
        conv = await self.find_or_create(name_or_phone)
        if not conv:
            raise RuntimeError(f"No conversation found for: {name_or_phone}")
        return await self.send(conv["id"], text)

    async def archive_many(self, conversation_ids: list[str]) -> list[str]:
        """Archive multiple conversations. -> [archived_ids]"""
        archived = []
        for cid in conversation_ids:
            try:
                await self.archive(cid)
                archived.append(cid)
            except Exception as e:
                print(f"  skip {cid}: {e}")
        return archived

    async def delete_many(self, conversation_ids: list[str]) -> list[str]:
        """Delete multiple conversations (gated on single confirm). -> [deleted_ids]"""
        convs = await self.conversations()
        names = []
        for cid in conversation_ids:
            c = next((c for c in convs if c["id"] == cid), None)
            names.append(c["name"] if c else cid)
        ok = await self._tab.confirm(
            f"Delete {len(conversation_ids)} conversations? ({', '.join(names[:5])}{'...' if len(names) > 5 else ''})"
        )
        if not ok:
            raise RuntimeError("Cancelled")
        deleted = []
        for cid in conversation_ids:
            try:
                await self._tab.js(f"""
(async () => {{
    const svc = window._msgService;
    const sh = window._shInstance;
    let state = null;
    sh.select(x => x).subscribe(val => {{ state = val; }}).unsubscribe();
    let conv = null;
    function walk(node, depth) {{
        if (!node || depth > 10 || conv) return;
        if (typeof node !== 'object') return;
        if (node.id === {repr(cid)}) {{ conv = node; return; }}
        for (const key of Object.keys(node)) {{
            const child = node[key];
            if (child && typeof child === 'object') walk(child, depth + 1);
        }}
    }}
    walk(state.conversations.conversations.map.root, 0);
    if (conv) await svc.deleteConversation(conv);
}})()
""")
                deleted.append(cid)
            except Exception as e:
                print(f"  skip {cid}: {e}")
        return deleted

    async def cleanup(self, dry_run: bool = True) -> list[dict]:
        """Find conversations that look like spam/verification codes. -> [{id, name, last_message}]"""
        convs = await self.conversations()
        junk = []
        junk_patterns = [
            "verification code", "code:", "kode:", "your code", "engangskode",
            "do not share", "don't share",
        ]
        for c in convs:
            body = c.get("last_message", "").lower()
            if any(p in body for p in junk_patterns):
                junk.append({"id": c["id"], "name": c["name"], "last_message": c["last_message"][:80]})
        if not dry_run and junk:
            ids = [j["id"] for j in junk]
            await self.archive_many(ids)
        return junk

    # --- ADB: full message history ---

    def dump(self) -> int:
        """Pull all SMS + MMS/RCS from phone via ADB into local SQLite. -> message count"""
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

        db = sqlite3.connect(_DB_PATH)
        db.execute("""CREATE TABLE messages (
            id TEXT, address TEXT, body TEXT, date INTEGER,
            type INTEGER, source TEXT,
            PRIMARY KEY (id, source)
        )""")

        # SMS
        sms = subprocess.run(
            [_ADB, "shell", "content", "query", "--uri", "content://sms",
             "--projection", "_id:address:body:date:type"],
            capture_output=True, text=True, timeout=30,
        )
        sms_n = 0
        for line in sms.stdout.split("\n"):
            m = re.match(
                r"Row: \d+ _id=(\d+), address=(.*?), body=(.*), date=(\d+), type=(\d+)$",
                line,
            )
            if m:
                db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)",
                    (m[1], m[2], m[3], int(m[4]), int(m[5]), "sms"),
                )
                sms_n += 1

        # MMS/RCS text parts
        parts = subprocess.run(
            [_ADB, "shell", "content", "query", "--uri", "content://mms/part",
             "--projection", "_id:mid:ct:text"],
            capture_output=True, text=True, timeout=30,
        )
        mms_texts: dict[str, str] = {}
        for line in parts.stdout.split("\n"):
            m = re.match(r"Row: \d+ _id=(\d+), mid=(\d+), ct=(.*?), text=(.*)", line)
            if m and m[3] == "text/plain":
                mms_texts[m[2]] = m[4]

        # MMS metadata + thread-based address resolution
        mms = subprocess.run(
            [_ADB, "shell", "content", "query", "--uri", "content://mms",
             "--projection", "_id:date:msg_box:thread_id"],
            capture_output=True, text=True, timeout=10,
        )
        # Build thread_id -> address map from SMS (threads are shared)
        thread_addr: dict[str, str] = {}
        for line in sms.stdout.split("\n"):
            tm = re.match(r"Row: \d+ _id=\d+, address=(.*?), body=.*, date=\d+, type=\d+$", line)
            if not tm:
                continue
            # Re-parse with thread_id
        sms_threads = subprocess.run(
            [_ADB, "shell", "content", "query", "--uri", "content://sms",
             "--projection", "thread_id:address"],
            capture_output=True, text=True, timeout=30,
        )
        for line in sms_threads.stdout.split("\n"):
            tm = re.match(r"Row: \d+ thread_id=(\d+), address=(.+)", line)
            if tm and tm[1] not in thread_addr:
                thread_addr[tm[1]] = tm[2]

        mms_n = 0
        for line in mms.stdout.split("\n"):
            m = re.match(
                r"Row: \d+ _id=(\d+), date=(\d+), msg_box=(\d+), thread_id=(\d+)", line
            )
            if m:
                text = mms_texts.get(m[1], "")
                if not text:
                    continue
                addr = thread_addr.get(m[4], "")
                db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)",
                    (f"mms_{m[1]}", addr, text, int(m[2]) * 1000,
                     1 if m[3] == "1" else 2, "mms"),
                )
                mms_n += 1

        db.execute("CREATE INDEX IF NOT EXISTS idx_body ON messages(body)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_addr ON messages(address)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_date ON messages(date)")
        db.commit()
        db.close()
        self._db = None
        return sms_n + mms_n

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            if not os.path.exists(_DB_PATH):
                raise RuntimeError("No dump — call gm.dump() first")
            self._db = sqlite3.connect(_DB_PATH)
            self._db.row_factory = sqlite3.Row
        return self._db

    def search_history(self, query: str, limit: int = 20) -> list[dict]:
        """Search all dumped messages by text content. -> [{address, body, date, type, source}]"""
        db = self._get_db()
        rows = db.execute(
            "SELECT * FROM messages WHERE body LIKE ? ORDER BY date DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [
            {**dict(r), "date_str": datetime.fromtimestamp(r["date"] / 1000).strftime("%Y-%m-%d %H:%M")}
            for r in rows
        ]

    def history_from(self, address: str, limit: int = 50) -> list[dict]:
        """Get messages from a specific sender/address. -> [{body, date, type}]"""
        db = self._get_db()
        rows = db.execute(
            "SELECT * FROM messages WHERE address LIKE ? ORDER BY date DESC LIMIT ?",
            (f"%{address}%", limit),
        ).fetchall()
        return [
            {**dict(r), "date_str": datetime.fromtimestamp(r["date"] / 1000).strftime("%Y-%m-%d %H:%M")}
            for r in rows
        ]

    def senders(self) -> list[dict]:
        """List all unique senders with message counts. -> [{address, count, latest}]"""
        db = self._get_db()
        rows = db.execute("""
            SELECT address, COUNT(*) as count, MAX(date) as latest
            FROM messages WHERE address != ''
            GROUP BY address ORDER BY latest DESC
        """).fetchall()
        return [
            {"address": r["address"], "count": r["count"],
             "latest": datetime.fromtimestamp(r["latest"] / 1000).strftime("%Y-%m-%d")}
            for r in rows
        ]
