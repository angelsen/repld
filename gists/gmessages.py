"""Google Messages — SMS & RCS via ADB, web for sends and management."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from datetime import datetime

import repld

__repld_usage__ = "gm = GMessages()"

_ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
_DB_PATH = "/tmp/gmessages.db"


def _adb(*args: str) -> str:
    r = subprocess.run([_ADB, *args], capture_output=True, text=True, timeout=30)
    return r.stdout


class GMessages:
    """Google Messages — ADB-first, web for writes.

    ADB: full SMS + RCS history (including E2EE), search, compose.
    Web: auto-send, archive, delete, contacts. Requires browser tab.
    """

    def __init__(self) -> None:
        self._db: sqlite3.Connection | None = None
        self._tab = None

    # --- ADB: read, search, compose ---

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
        sms_out = _adb("shell", "content", "query", "--uri", "content://sms",
                        "--projection", "_id:address:body:date:type")
        sms_n = 0
        for line in sms_out.split("\n"):
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
        parts_out = _adb("shell", "content", "query", "--uri", "content://mms/part",
                          "--projection", "_id:mid:ct:text")
        mms_texts: dict[str, str] = {}
        for line in parts_out.split("\n"):
            m = re.match(r"Row: \d+ _id=(\d+), mid=(\d+), ct=(.*?), text=(.*)", line)
            if m and m[3] == "text/plain":
                mms_texts[m[2]] = m[4]

        # MMS metadata
        mms_out = _adb("shell", "content", "query", "--uri", "content://mms",
                        "--projection", "_id:date:msg_box:thread_id")

        # Thread -> address mapping (threads shared between SMS and MMS)
        thread_addr: dict[str, str] = {}
        threads_out = _adb("shell", "content", "query", "--uri", "content://sms",
                            "--projection", "thread_id:address")
        for line in threads_out.split("\n"):
            tm = re.match(r"Row: \d+ thread_id=(\d+), address=(.+)", line)
            if tm and tm[1] not in thread_addr:
                thread_addr[tm[1]] = tm[2]

        mms_n = 0
        for line in mms_out.split("\n"):
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
            self._db = sqlite3.connect(_DB_PATH, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
        return self._db

    def _fmt(self, rows) -> list[dict]:
        return [
            {**dict(r), "date_str": datetime.fromtimestamp(r["date"] / 1000).strftime("%Y-%m-%d %H:%M")}
            for r in rows
        ]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search all messages by text content. -> [{address, body, date, type, source, date_str}]"""
        db = self._get_db()
        return self._fmt(db.execute(
            "SELECT * FROM messages WHERE body LIKE ? ORDER BY date DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall())

    def history(self, address: str, limit: int = 50) -> list[dict]:
        """Get messages from/to an address. -> [{address, body, date, type, source, date_str}]"""
        db = self._get_db()
        return self._fmt(db.execute(
            "SELECT * FROM messages WHERE address LIKE ? ORDER BY date DESC LIMIT ?",
            (f"%{address}%", limit),
        ).fetchall())

    def senders(self) -> list[dict]:
        """List unique senders with message counts. -> [{address, count, latest}]"""
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

    def compose(self, phone: str, text: str) -> None:
        """Open compose UI on phone with pre-filled message. -> None"""
        _adb("shell", f'am start -a android.intent.action.SENDTO -d smsto:{phone} --es sms_body "{text}"')

    # --- Web: send, archive, delete (requires browser tab) ---

    async def web(self) -> "GMessages":
        """Connect to Google Messages web for send/archive/delete. -> self"""
        if self._tab:
            return self
        try:
            self._tab = await repld.browser.get("*messages.google.com*")
        except RuntimeError:
            self._tab = await repld.browser.open("https://messages.google.com/web/conversations")
            await self._tab.wait_for("role=listbox", timeout=15)
        await self._tab.pin("Google Messages — repld integration")
        await self._ensure_service()
        return self

    async def _ensure_service(self) -> None:
        has = await self._tab.js("!!window._shInstance")
        if has:
            return
        await self._tab.js("""
(() => {
    const mw = window.default_mw;
    const SHClass = mw.SH;
    window._origSHSend = SHClass.prototype.sendMessage;
    const origSH = SHClass.prototype.sendMessage;
    SHClass.prototype.sendMessage = function(...args) {
        window._shInstance = this;
        window._msgService = this.Ib;
        SHClass.prototype.sendMessage = origSH;
        return origSH.apply(this, args);
    };
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

    def _require_web(self) -> None:
        if not self._tab:
            raise RuntimeError("Web not connected — call await gm.web() first")

    async def conversations(self) -> list[dict]:
        """List conversations from web store. -> [{id, name, phone, last_message, timestamp, e2ee}]"""
        self._require_web()
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
                id: node.id, name: node.name,
                phone: other[0]?.Ab?.id || '',
                last_message: node.kd?.text || '',
                last_sender: node.kd?.vj || '',
                timestamp: node.kd?.Md || node.Md,
                e2ee: !!node.hasBeenE2ee, type: node.type,
                muted: !!node.isMuted
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
    return convs;
})()
""")

    async def contacts(self) -> list[dict]:
        """List synced contacts from web store. -> [{name, phone, id}]"""
        self._require_web()
        return await self._tab.js("""
(async () => {
    const sh = window._shInstance;
    if (!sh) throw new Error('Service not captured — send a message first');
    let state = null;
    sh.select(x => x).subscribe(val => { state = val; }).unsubscribe();
    if (state.contacts.contacts.map.size === 0) {
        const btn = Array.from(document.querySelectorAll('a'))
            .find(a => a.textContent.includes('Start chat'));
        if (btn) {
            btn.click();
            await new Promise(r => setTimeout(r, 2000));
            const back = document.querySelector('button[aria-label="Back"]') ||
                Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('\u2190'));
            if (back) back.click();
            await new Promise(r => setTimeout(r, 500));
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

    async def send(self, conversation_id: str, text: str) -> dict:
        """Send a message via web (auto-send, pill-gated). -> {ok, conversationId}"""
        self._require_web()
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
    const convMap = state.conversations.conversations;
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
    walk(convMap.map.root, 0);
    if (!conv) throw new Error('Conversation not found: ' + convId);
    const tmpId = 'tmp_' + Math.floor(Math.random() * 999999999999);
    const message = {{
        conversationId: convId, id: tmpId, Sf: 1, Ed: true, Yf: false,
        Hb: {{0: {{order: 9007199254740991, Yb: "0", type: "text", text: {repr(text)}}}}},
        le: conv.Sl || "2", status: 1, timestampMs: Date.now(),
        type: conv.type || 1, yf: tmpId
    }};
    await window._origSHSend.call(sh, {{message, Ka: conv}});
    return {{ok: true, conversationId: convId}};
}})()
""")

    async def send_to(self, name_or_phone: str, text: str) -> dict:
        """Find conversation by name/phone and send via web. -> {ok, conversationId}"""
        self._require_web()
        convs = await self.conversations()
        q = name_or_phone.lower().replace(" ", "")
        conv = next((c for c in convs
                     if q in c["name"].lower() or q in c.get("phone", "").replace(" ", "")),
                    None)
        if not conv:
            raise RuntimeError(f"No conversation found for: {name_or_phone}")
        return await self.send(conv["id"], text)

    async def _conv_action(self, conversation_id: str, method: str) -> None:
        self._require_web()
        await self._tab.js(f"""
(() => {{
    const svc = window._msgService;
    if (!svc) throw new Error('Service not captured');
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
    if (!conv) throw new Error('Conversation not found');
    svc.{method}(conv);
}})()
""")

    async def archive(self, conversation_id: str) -> None:
        """Archive a conversation via web. -> None"""
        await self._conv_action(conversation_id, "archiveConversation")

    async def delete(self, conversation_id: str) -> None:
        """Move conversation to bin via web. -> None"""
        await self._conv_action(conversation_id, "deleteConversation")

    async def permanent_delete(self, conversation_id: str) -> None:
        """Permanently delete a conversation via web (bypasses bin). -> None"""
        self._require_web()
        await self._tab.js(f"""
(() => {{
    const ji = window._jIInstance;
    if (!ji) throw new Error('jI not captured');
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
    if (!conv) throw new Error('Conversation not found');
    window._msgService.deleteConversation(conv);
    setTimeout(() => {{ ji.flags.ab = origAb; }}, 100);
}})()
""")

    async def archive_many(self, conversation_ids: list[str]) -> list[str]:
        """Archive multiple conversations via web. -> [archived_ids]"""
        archived = []
        for cid in conversation_ids:
            try:
                await self.archive(cid)
                archived.append(cid)
            except Exception as e:
                print(f"  skip {cid}: {e}")
        return archived

    async def cleanup(self, dry_run: bool = True) -> list[dict]:
        """Find junk conversations (verification codes etc). -> [{id, name, last_message}]"""
        self._require_web()
        convs = await self.conversations()
        junk_patterns = [
            "verification code", "code:", "kode:", "your code", "engangskode",
            "do not share", "don't share",
        ]
        junk = [
            {"id": c["id"], "name": c["name"], "last_message": c["last_message"][:80]}
            for c in convs
            if any(p in c.get("last_message", "").lower() for p in junk_patterns)
        ]
        if not dry_run and junk:
            await self.archive_many([j["id"] for j in junk])
        return junk
