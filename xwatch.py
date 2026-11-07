#!/usr/bin/env python3
"""
xwatch — X(Twitter) の公開アカウントを定期的に巡回し、最新投稿をローカルに貯めて
美しいダッシュボードで眺めるための個人用ツール。

  起動:      python xwatch.py            (ブラウザで http://127.0.0.1:8765 が開きます)
  一回だけ:  python xwatch.py --once     (全アカウントを1回取得して終了)
  AI連携:    python xwatch.py --mcp      (MCP サーバーとして stdio 起動 / 起動中は /mcp と /feed.md も)
  オプション: --port 9000 / --host 127.0.0.1 / --no-browser / --data ./data

Python 3.9+ の標準ライブラリだけで動きます（pip install 不要）。Windows では setup.bat で自動起動とショートカットを設定できます。
ログインなしで見える範囲（各アカウント直近数件）を読むため、X側の仕様変更で
取得できなくなる場合があります。個人利用・低頻度（既定30分、最短10分）で使ってください。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "1.1.0-parallel"

APP_DIR = Path(__file__).resolve().parent
DASHBOARD = APP_DIR / "dashboard.html"
# バージョン別のフォルダを並べている場合は、一つ上の共通の保存データを参照する。
# それ以外（ダウンロードしたフォルダ単体など）はこのフォルダの中の data/ に保存する。
DEFAULT_DATA_DIR = (APP_DIR.parent / "data"
                    if (APP_DIR.parent / "data" / "xwatch.db").is_file() else APP_DIR / "data")

MIN_INTERVAL_MIN = 10
MAX_INTERVAL_MIN = 24 * 60
DEFAULT_INTERVAL_MIN = 30
MAX_ACCOUNTS = 500
FETCH_WORKERS = 10
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

log = logging.getLogger("xwatch")


# ──────────────────────────────────────────────────────────────────────────────
# 取得・解析
# ──────────────────────────────────────────────────────────────────────────────
class FetchError(Exception):
    """取得に失敗した（kind: not_found / protected / blocked / network / parse）"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def http_get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8,ja;q=0.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FetchError("not_found", "アカウントが見つかりません (404)")
        if e.code in (429, 403):
            raise FetchError("blocked", f"Xにアクセスを制限されました (HTTP {e.code})")
        raise FetchError("network", f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError("network", f"通信エラー: {getattr(e, 'reason', e)}")


_RESULT_RE = re.compile(r'kind:"Result\.Ok",value:"')


def _js_string_at(src: str, start: int) -> str:
    """src[start] から始まる JS 文字列リテラルの中身（閉じ " の手前まで）を返す。"""
    j, n = start, len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return src[start:j]
        j += 1
    raise ValueError("unterminated string")


def extract_documents(html: str) -> list[dict]:
    """X のプロフィールHTMLに埋め込まれた GraphQL 応答(JSON)をすべて取り出す。"""
    docs = []
    for m in _RESULT_RE.finditer(html):
        try:
            raw = _js_string_at(html, m.end())
            docs.append(json.loads(json.loads('"' + raw + '"')))
        except (ValueError, json.JSONDecodeError):
            continue
    return docs


def _big_avatar(url: str | None) -> str:
    return (url or "").replace("_normal.", "_400x400.")


def _expand_text(text: str, url_entities: list, media: list) -> str:
    """t.co 短縮URLを展開し、末尾のメディア用 t.co を取り除く。"""
    for u in url_entities or []:
        short, exp = u.get("url"), u.get("expanded_url")
        if short and exp:
            text = text.replace(short, exp)
    media_urls = {m.get("url") for m in media if m.get("url")}
    for mu in media_urls:
        text = text.replace(mu, "")
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text) if media else text
    return text.strip()


def _parse_media(items: list) -> list[dict]:
    out = []
    for m in items or []:
        mtype = m.get("type") or "photo"
        entry = {
            "type": mtype,
            "thumb": m.get("media_url_https"),
            "url": m.get("expanded_url"),
            "w": (m.get("original_info") or {}).get("width"),
            "h": (m.get("original_info") or {}).get("height"),
            "_tco": m.get("url"),
        }
        if mtype in ("video", "animated_gif"):
            variants = [v for v in ((m.get("video_info") or {}).get("variants") or [])
                        if v.get("content_type") == "video/mp4"]
            if variants:
                entry["video"] = max(variants, key=lambda v: v.get("bitrate") or 0)["url"]
            entry["duration_ms"] = (m.get("video_info") or {}).get("duration_millis")
        out.append(entry)
    return out


def _user_brief(user_results: dict) -> dict:
    u = (user_results or {}).get("result") or {}
    core = u.get("core") or {}
    return {
        "screen_name": core.get("screen_name") or "",
        "name": core.get("name") or "",
        "avatar": _big_avatar((u.get("avatar") or {}).get("image_url")),
        "verified": bool((u.get("verification") or {}).get("is_blue_verified")),
    }


def _parse_tweet(t: dict, *, nested: bool = False) -> dict | None:
    if not t:
        return None
    if t.get("__typename") == "TweetWithVisibilityResults":
        t = t.get("tweet") or {}
    details = t.get("details") or {}
    tid = t.get("rest_id")
    text = details.get("full_text")
    if not tid or text is None:
        return None
    raw_media = t.get("media_entities2") or []
    media = _parse_media(raw_media)
    counts = t.get("counts") or {}
    views = (t.get("views") or {}).get("count")
    author = _user_brief((t.get("core") or {}).get("user_results"))
    created_ms = details.get("created_at_ms") or (((int(tid) >> 22) + 1288834974657))
    tweet = {
        "id": tid,
        "author": author,
        "created_at": int(created_ms) // 1000,
        "text": _expand_text(text, t.get("url_entities") or [], [{"url": m["_tco"]} for m in media]),
        "lang": (t.get("legacy") or {}).get("lang"),
        "counts": {
            "likes": counts.get("favorite_count"),
            "replies": counts.get("reply_count"),
            "reposts": counts.get("retweet_count"),
            "quotes": counts.get("quote_count"),
            "bookmarks": counts.get("bookmark_count"),
            "views": int(views) if views and str(views).isdigit() else None,
        },
        "media": [{k: v for k, v in m.items() if not k.startswith("_")} for m in media],
        "urls": [{"url": u.get("expanded_url"), "display": u.get("display_url")}
                 for u in (t.get("url_entities") or []) if u.get("expanded_url")],
        "permalink": f"https://x.com/{author['screen_name'] or 'i'}/status/{tid}",
    }
    if not nested:
        q = (t.get("quoted_tweet_results") or {}).get("result")
        tweet["quoted"] = _parse_tweet(q, nested=True) if q else None
    return tweet


def _walk(obj, key):
    """入れ子の dict/list から key を持つ dict を順に返す。"""
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from _walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)


def parse_profile(html: str, handle: str) -> tuple[dict, list[dict]]:
    """HTML から (プロフィール, 投稿リスト) を返す。"""
    docs = extract_documents(html)
    if not docs:
        if "This account doesn’t exist" in html or "This account doesn't exist" in html:
            raise FetchError("not_found", "アカウントが存在しません")
        raise FetchError("parse", "ページから投稿データを見つけられませんでした（X側の仕様変更の可能性）")

    profile: dict = {"handle": handle}
    tweets: dict[str, dict] = {}
    pinned_ids: set[str] = set()

    for d in docs:
        root = ((d.get("data") or {}).get("user_result_by_screen_name") or {}).get("result") or {}
        if root.get("__typename") == "UserUnavailable":
            raise FetchError("not_found", root.get("message") or "利用できないアカウントです")
        if "core" in root:
            core = root.get("core") or {}
            profile.update({
                "handle": core.get("screen_name") or handle,
                "name": core.get("name") or "",
                "avatar": _big_avatar((root.get("avatar") or {}).get("image_url")),
                "banner": (root.get("banner") or {}).get("image_url") or "",
                "bio": (root.get("profile_bio") or {}).get("description") or "",
                "followers": (root.get("relationship_counts") or {}).get("followers"),
                "following": (root.get("relationship_counts") or {}).get("following"),
                "tweets": (root.get("tweet_counts") or {}).get("tweets"),
                "verified": bool((root.get("verification") or {}).get("is_blue_verified")),
                "protected": bool((root.get("privacy") or {}).get("protected")),
            })
        for ins in _walk(root, "__typename"):
            if ins.get("__typename") == "TimelinePinEntry":
                for tr in _walk(ins, "tweet_results"):
                    rid = (tr["tweet_results"] or {}).get("rest_id")
                    if rid:
                        pinned_ids.add(rid)
        for holder in _walk(root, "tweet_results"):
            res = (holder.get("tweet_results") or {}).get("result")
            tw = _parse_tweet(res) if res else None
            if not tw:
                continue
            # 引用元など他人の投稿は単独では扱わない
            if tw["author"]["screen_name"].lower() != profile.get("handle", handle).lower():
                continue
            tw["pinned"] = tw["id"] in pinned_ids
            tweets.setdefault(tw["id"], tw)

    if profile.get("protected") and not tweets:
        raise FetchError("protected", "鍵アカウントのため取得できません")
    if not tweets and not profile.get("name"):
        raise FetchError("parse", "投稿を解析できませんでした（X側の仕様変更の可能性）")
    out = sorted(tweets.values(), key=lambda t: int(t["id"]), reverse=True)
    return profile, out


def fetch_account(handle: str) -> tuple[dict, list[dict]]:
    html = http_get(f"https://x.com/{urllib.parse.quote(handle)}")
    return parse_profile(html, handle)


# ──────────────────────────────────────────────────────────────────────────────
# 保存
# ──────────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  handle      TEXT PRIMARY KEY COLLATE NOCASE,
  added_at    INTEGER NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  profile     TEXT,
  last_try    INTEGER,
  last_ok     INTEGER,
  last_error  TEXT,
  error_kind  TEXT,
  fail_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
  id          TEXT PRIMARY KEY,
  handle      TEXT NOT NULL COLLATE NOCASE,
  created_at  INTEGER NOT NULL,
  first_seen  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  is_read     INTEGER NOT NULL DEFAULT 0,
  starred     INTEGER NOT NULL DEFAULT 0,
  text        TEXT NOT NULL,
  data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_handle_time ON posts(handle, created_at DESC);
CREATE INDEX IF NOT EXISTS posts_time ON posts(created_at DESC);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at INTEGER, finished_at INTEGER, trigger TEXT,
  ok INTEGER, failed INTEGER, new_posts INTEGER
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # settings
    def get(self, key: str, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value) -> None:
        with self._lock:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()

    # accounts
    def accounts(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("""
              SELECT a.*, (SELECT COUNT(*) FROM posts p WHERE p.handle=a.handle AND p.is_read=0) AS unread,
                          (SELECT COUNT(*) FROM posts p WHERE p.handle=a.handle) AS total,
                          (SELECT MAX(created_at) FROM posts p WHERE p.handle=a.handle) AS latest
              FROM accounts a ORDER BY a.added_at""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["profile"] = json.loads(d["profile"]) if d["profile"] else None
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def add_account(self, handle: str) -> bool:
        with self._lock:
            n = self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if n >= MAX_ACCOUNTS:
                raise ValueError(f"登録できるのは最大{MAX_ACCOUNTS}件です")
            cur = self.db.execute("INSERT OR IGNORE INTO accounts(handle, added_at) VALUES(?,?)",
                                  (handle, int(time.time())))
            self.db.commit()
            return cur.rowcount > 0

    def remove_account(self, handle: str, purge: bool) -> None:
        with self._lock:
            self.db.execute("DELETE FROM accounts WHERE handle=?", (handle,))
            if purge:
                self.db.execute("DELETE FROM posts WHERE handle=?", (handle,))
            self.db.commit()

    def set_enabled(self, handle: str, enabled: bool) -> None:
        with self._lock:
            self.db.execute("UPDATE accounts SET enabled=? WHERE handle=?", (int(enabled), handle))
            self.db.commit()

    def record_ok(self, handle: str, profile: dict) -> None:
        now = int(time.time())
        with self._lock:
            self.db.execute("""UPDATE accounts SET profile=?, last_try=?, last_ok=?, last_error=NULL,
                               error_kind=NULL, fail_count=0 WHERE handle=?""",
                            (json.dumps(profile, ensure_ascii=False), now, now, handle))
            self.db.commit()

    def record_error(self, handle: str, err: FetchError) -> int:
        with self._lock:
            self.db.execute("""UPDATE accounts SET last_try=?, last_error=?, error_kind=?,
                               fail_count=fail_count+1 WHERE handle=?""",
                            (int(time.time()), str(err), err.kind, handle))
            self.db.commit()
            r = self.db.execute("SELECT fail_count FROM accounts WHERE handle=?", (handle,)).fetchone()
        return r["fail_count"] if r else 0

    # posts
    def upsert_posts(self, handle: str, tweets: list[dict], mark_read: bool) -> int:
        now = int(time.time())
        new = 0
        with self._lock:
            for t in tweets:
                data = json.dumps(t, ensure_ascii=False)
                cur = self.db.execute("UPDATE posts SET data=?, text=?, updated_at=? WHERE id=?",
                                      (data, t["text"], now, t["id"]))
                if cur.rowcount == 0:
                    self.db.execute("""INSERT INTO posts(id, handle, created_at, first_seen, updated_at,
                                       is_read, text, data) VALUES(?,?,?,?,?,?,?,?)""",
                                    (t["id"], handle, t["created_at"], now, now, int(mark_read),
                                     t["text"], data))
                    new += 1
            self.db.commit()
        return new

    def posts(self, handle: str | None, q: str | None, unread: bool, starred: bool,
              before: str | None, limit: int) -> list[dict]:
        sql, args = ["SELECT * FROM posts WHERE 1=1"], []
        if handle:
            sql.append("AND handle=?"); args.append(handle)
        if q:
            sql.append("AND text LIKE ? ESCAPE '\\'")
            args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if unread:
            sql.append("AND is_read=0")
        if starred:
            sql.append("AND starred=1")
        if before:
            sql.append("AND CAST(id AS INTEGER) < CAST(? AS INTEGER)"); args.append(before)
        sql.append("ORDER BY CAST(id AS INTEGER) DESC LIMIT ?"); args.append(limit)
        with self._lock:
            rows = self.db.execute(" ".join(sql), args).fetchall()
        return [self._row_to_post(r) for r in rows]

    def _row_to_post(self, r) -> dict:
        d = json.loads(r["data"])
        d.update(handle=r["handle"], first_seen=r["first_seen"], is_read=bool(r["is_read"]),
                 starred=bool(r["starred"]))
        return d

    def post(self, pid: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        return self._row_to_post(r) if r else None

    def new_posts(self, after_rowid: int, handle: str | None, limit: int) -> tuple[list[dict], int, int]:
        """保存順（rowid）で after_rowid より後の投稿を古い順に limit 件。(posts, 最後のrowid, 残り件数)"""
        where, args = "rowid > ?", [after_rowid]
        if handle:
            where += " AND handle=?"; args.append(handle)
        with self._lock:
            rows = self.db.execute(f"SELECT rowid AS rid, * FROM posts WHERE {where} ORDER BY rowid LIMIT ?",
                                   args + [limit]).fetchall()
            total = self.db.execute(f"SELECT COUNT(*) FROM posts WHERE {where}", args).fetchone()[0]
        last = rows[-1]["rid"] if rows else after_rowid
        return [self._row_to_post(r) for r in rows], last, total - len(rows)

    def rowid_bounds(self, seen_after: int) -> tuple[int, int]:
        """(first_seen >= seen_after の最小rowid-1, 全体の最大rowid)"""
        with self._lock:
            r = self.db.execute("SELECT (SELECT MIN(rowid) FROM posts WHERE first_seen >= ?) lo, "
                                "(SELECT MAX(rowid) FROM posts) hi", (seen_after,)).fetchone()
        hi = r["hi"] or 0
        return ((r["lo"] - 1) if r["lo"] is not None else hi), hi

    def mark_read(self, handle: str | None = None, ids: list[str] | None = None) -> int:
        with self._lock:
            if ids:
                q = ",".join("?" * len(ids))
                cur = self.db.execute(f"UPDATE posts SET is_read=1 WHERE is_read=0 AND id IN ({q})", ids)
            elif handle:
                cur = self.db.execute("UPDATE posts SET is_read=1 WHERE is_read=0 AND handle=?", (handle,))
            else:
                cur = self.db.execute("UPDATE posts SET is_read=1 WHERE is_read=0")
            self.db.commit()
            return cur.rowcount

    def star(self, pid: str, on: bool) -> None:
        with self._lock:
            self.db.execute("UPDATE posts SET starred=? WHERE id=?", (int(on), pid))
            self.db.commit()

    def stats(self) -> dict:
        with self._lock:
            r = self.db.execute("""SELECT COUNT(*) total, SUM(is_read=0) unread, SUM(starred) starred,
                                   SUM(first_seen > ?) today FROM posts""",
                                (int(time.time()) - 86400,)).fetchone()
        return {k: (r[k] or 0) for k in ("total", "unread", "starred", "today")}

    def log_run(self, started: int, trigger: str, ok: int, failed: int, new: int) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO runs(started_at, finished_at, trigger, ok, failed, new_posts)
                               VALUES(?,?,?,?,?,?)""", (started, int(time.time()), trigger, ok, failed, new))
            self.db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 200)")
            self.db.commit()

    def runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# スケジューラ
# ──────────────────────────────────────────────────────────────────────────────
class Scheduler(threading.Thread):
    def __init__(self, store: Store):
        super().__init__(daemon=True, name="xwatch-scheduler")
        self.store = store
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._manual: list[str | None] = []   # None = 全件, str = 1アカウント
        self._mlock = threading.Lock()
        self.running = False
        self.current: str | None = None
        self.progress = (0, 0)
        self.next_run: float | None = None
        self.last_run: float | None = self.store.get("last_run")
        self.events: list[dict] = []          # 新着イベント（ダッシュボード通知用）
        self.event_seq = 0

    # 設定
    @property
    def interval_min(self) -> int:
        return int(self.store.get("interval_min", DEFAULT_INTERVAL_MIN))

    @property
    def paused(self) -> bool:
        return bool(self.store.get("paused", False))

    def plan_next(self) -> None:
        if self.paused:
            self.next_run = None
            return
        # 期限を過ぎた予定は未来へずらさず、次のループですぐ実行する。
        self.next_run = (self.last_run + self.interval_min * 60
                         if self.last_run is not None else time.time())

    def trigger(self, handle: str | None = None) -> None:
        with self._mlock:
            if handle is None:
                self._manual = [None]
            elif None not in self._manual and handle not in self._manual:
                self._manual.append(handle)
        self._wake.set()

    def reconfigure(self) -> None:
        self.plan_next()
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    # ループ
    def run(self) -> None:
        self.plan_next()
        while not self._stop_event.is_set():
            with self._mlock:
                manual, self._manual = self._manual, []
            if manual:
                handles = None if None in manual else manual
                self.cycle("manual", handles)
                continue
            self.last_run = self.store.get("last_run") or self.last_run
            self.plan_next()  # 別プロセス（MCP stdio）からの設定変更も反映
            if self.next_run and time.time() >= self.next_run:
                self.cycle("schedule", None)
                continue
            wait = 15.0 if not self.next_run else max(0.5, min(15.0, self.next_run - time.time()))
            self._wake.wait(wait)
            self._wake.clear()

    def cycle(self, trigger: str, only: list[str] | None) -> None:
        accounts = [a for a in self.store.accounts() if a["enabled"]]
        if only:
            wanted = {h.lower() for h in only}
            accounts = [a for a in self.store.accounts() if a["handle"].lower() in wanted]
        started = int(time.time())
        ok = failed = new_total = 0
        self.running = True
        self.progress = (0, len(accounts))
        try:
            # 失敗が続くアカウントを除き、最大10件ずつ取得する。
            eligible = []
            for acc in accounts:
                if trigger == "schedule" and acc["fail_count"] >= 3 and acc["last_try"]:
                    backoff = min(2 ** (acc["fail_count"] - 2), 16) * self.interval_min * 60
                    if time.time() - acc["last_try"] < backoff:
                        continue
                eligible.append(acc)
            self.progress = (0, len(eligible))
            done = 0
            with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
                for offset in range(0, len(eligible), FETCH_WORKERS):
                    if self._stop_event.is_set():
                        break
                    batch = eligible[offset:offset + FETCH_WORKERS]
                    futures = {pool.submit(fetch_account, acc["handle"]): acc for acc in batch}
                    blocked = False
                    for future in as_completed(futures):
                        acc = futures[future]
                        handle = acc["handle"]
                        self.current = handle
                        try:
                            profile, tweets = future.result()
                            self.store.record_ok(handle, profile)
                            new = self.store.upsert_posts(handle, tweets, mark_read=False)
                            ok += 1
                            new_total += new
                            if new and acc["last_ok"] is not None:
                                self._emit(handle, profile, new, tweets[:new])
                            log.info("@%s: %d件取得 / 新着%d", handle, len(tweets), new)
                        except FetchError as e:
                            failed += 1
                            self.store.record_error(handle, e)
                            log.warning("@%s: %s", handle, e)
                            if e.kind == "blocked":
                                blocked = True
                        except Exception as e:
                            failed += 1
                            self.store.record_error(handle, FetchError("parse", f"予期しないエラー: {e}"))
                            log.exception("@%s: unexpected", handle)
                        done += 1
                        self.progress = (done, len(eligible))
                    if blocked:
                        log.warning("アクセス制限を検知したため今回の巡回を中断します")
                        break
                    if offset + FETCH_WORKERS < len(eligible):
                        self._stop_event.wait(random.uniform(2.5, 6.0))
        finally:
            self.running = False
            self.current = None
            if not self._stop_event.is_set() and (trigger == "schedule" or only is None):
                self.last_run = time.time()
                self.store.set("last_run", self.last_run)
            self.store.log_run(started, trigger, ok, failed, new_total)
            self.plan_next()

    def _emit(self, handle: str, profile: dict, n: int, tweets: list[dict]) -> None:
        self.event_seq += 1
        self.events.append({
            "seq": self.event_seq, "handle": handle, "name": profile.get("name") or handle,
            "avatar": profile.get("avatar"), "count": n,
            "preview": (tweets[0]["text"][:120] if tweets else ""), "at": int(time.time()),
        })
        self.events = self.events[-50:]

    def state(self) -> dict:
        return {
            "running": self.running, "current": self.current,
            "progress": list(self.progress), "next_run": self.next_run,
            "last_run": self.last_run, "interval_min": self.interval_min,
            "paused": self.paused, "event_seq": self.event_seq,
        }


# ──────────────────────────────────────────────────────────────────────────────
# AI 向け: Markdown フィード と MCP サーバー
# ──────────────────────────────────────────────────────────────────────────────
MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MD_MAX_POSTS = 200


def _local_dt(ts: int) -> str:
    d = datetime.fromtimestamp(ts).astimezone()
    off = d.strftime("%z")
    zone = "JST" if off == "+0900" else f"UTC{off[:3]}:{off[3:]}"
    return d.strftime("%Y-%m-%d %H:%M ") + zone


def _md_escape_line(s: str) -> str:
    # 投稿本文の行頭が Markdown 見出し等として解釈されないよう引用ブロックに入れる
    return "\n".join("> " + ln if ln.strip() else ">" for ln in (s or "").splitlines()) or ">"


def _fmt_count(n) -> str | None:
    return None if n is None else f"{n:,}"


def post_to_markdown(p: dict, level: int = 3) -> str:
    a = p.get("author") or {}
    who = f"@{a.get('screen_name') or p.get('handle')}"
    name = a.get("name")
    head = f"{'#' * level} {who}{f'（{name}）' if name else ''} · {_local_dt(p['created_at'])}"
    out = [head, "", _md_escape_line(p.get("text", ""))]
    q = p.get("quoted")
    if q:
        qa = (q.get("author") or {}).get("screen_name") or "?"
        out += [">", f"> **引用 @{qa}:**", "\n".join("> " + ln for ln in _md_escape_line(q.get("text", "")).splitlines()),
                f"> — {q.get('permalink', '')}"]
    for m in p.get("media") or []:
        kind = {"photo": "画像", "video": "動画", "animated_gif": "GIF"}.get(m.get("type"), "メディア")
        out.append(f"- {kind}: {m.get('video') or m.get('thumb') or m.get('url')}")
    links = [u["url"] for u in p.get("urls") or [] if u.get("url")]
    if links:
        out.append("- リンク: " + " ".join(links))
    c = p.get("counts") or {}
    stats = [f"{lab} {_fmt_count(c.get(k))}" for k, lab in
             (("likes", "いいね"), ("reposts", "リポスト"), ("replies", "返信"), ("views", "表示"))
             if c.get(k) is not None]
    if stats:
        out.append("- " + " · ".join(stats))
    flags = [f for f, on in (("★スター", p.get("starred")), ("未読", p.get("is_read") is False)) if on]
    out.append(f"- URL: {p.get('permalink')} · ID: {p.get('id')}" + (f" · {' '.join(flags)}" if flags else ""))
    return "\n".join(out)


def build_feed_markdown(store: "Store", *, handle: str | None = None, hours: float | None = 24,
                        limit: int = 30, unread_only: bool = False, query: str | None = None,
                        starred_only: bool = False,
                        title: str | None = None, posts: list[dict] | None = None,
                        cond_note: str | None = None, footer: str | None = None) -> tuple[str, list[dict]]:
    """保存済み投稿を Markdown に整形して返す。(markdown, posts)。posts を渡すとそれを整形する。"""
    if limit is None or not 1 <= int(limit) <= MD_MAX_POSTS:
        raise ValueError(f"limit は1〜{MD_MAX_POSTS}で指定してください")
    if hours is not None and float(hours) < 0:
        raise ValueError("hours は0以上で指定してください")
    limit = int(limit)
    if posts is None:
        posts = store.posts(handle, query, unread_only, starred_only, None, MD_MAX_POSTS if hours else limit)
        if hours:
            cutoff = time.time() - float(hours) * 3600
            posts = [p for p in posts if p["created_at"] >= cutoff]
        posts = posts[:limit]
    else:
        posts = sorted(posts, key=lambda p: int(p["id"]), reverse=True)
    now = int(time.time())
    cond = []
    cond.append(f"@{handle}" if handle else "全アカウント")
    if hours:
        cond.append(f"直近{hours:g}時間")
    if query:
        cond.append(f"検索「{query}」")
    if unread_only:
        cond.append("未読のみ")
    if starred_only:
        cond.append("スターのみ")
    if cond_note:
        cond.append(cond_note)
    lines = [f"# {title or 'xwatch 最新投稿'}", "",
             f"- 生成: {_local_dt(now)}",
             f"- 条件: {' / '.join(cond)} · {len(posts)}件（新しい順）"]
    accs = store.accounts()
    last_ok = max((a["last_ok"] or 0 for a in accs), default=0)
    if last_ok:
        lines.append(f"- 最終取得: {_local_dt(last_ok)}")
    errs = [a["handle"] for a in accs if a["last_error"] and (not handle or a["handle"] == handle)]
    if errs:
        lines.append(f"- 取得エラー中: {', '.join('@' + h for h in errs)}（このアカウントの情報は古い可能性があります）")
    lines.append("")
    if not posts:
        lines.append("_該当する投稿はありません。_")
    for p in posts:
        lines += [post_to_markdown(p), ""]
    if footer:
        lines += ["---", footer, ""]
    return "\n".join(lines).rstrip() + "\n", posts


def accounts_markdown(store: "Store", sched: "Scheduler | None" = None) -> str:
    accs = store.accounts()
    lines = ["# xwatch 追跡アカウント", ""]
    if sched is not None:
        st = sched.state()
        lines.append(f"- 巡回間隔: {st['interval_min']}分 · 自動巡回: {'停止中' if st['paused'] else 'オン'}"
                     + (f" · 次回: {_local_dt(int(st['next_run']))}" if st.get("next_run") else ""))
    else:
        lines.append(f"- 巡回間隔: {store.get('interval_min', DEFAULT_INTERVAL_MIN)}分")
    lines += ["", "| アカウント | 名前 | 保存 | 未読 | 最新投稿 | 最終取得 | 状態 |", "|---|---|---:|---:|---|---|---|"]
    for a in accs:
        name = ((a["profile"] or {}).get("name") or "").replace("|", "／")
        state = "一時停止" if not a["enabled"] else (f"エラー: {a['error_kind']}" if a["last_error"] else "OK")
        lines.append(f"| @{a['handle']} | {name} | {a['total']} | {a['unread']} | "
                     f"{_local_dt(a['latest']) if a['latest'] else '–'} | "
                     f"{_local_dt(a['last_ok']) if a['last_ok'] else '–'} | {state} |")
    if not accs:
        lines.append("| （なし） | | | | | | |")
    return "\n".join(lines) + "\n"


def status_markdown(store: "Store", sched: "Scheduler | None" = None) -> str:
    st = store.stats()
    interval = int(store.get("interval_min", DEFAULT_INTERVAL_MIN))
    paused = bool(store.get("paused", False))
    last = store.get("last_run")
    lines = ["# xwatch の状態", "",
             f"- 巡回間隔: {interval}分 · 自動巡回: {'オフ' if paused else 'オン'}",
             f"- 前回の巡回: {_local_dt(int(last)) if last else '未実行'}"]
    if sched is not None:
        s = sched.state()
        if s.get("next_run"):
            lines.append(f"- 次回の巡回: {_local_dt(int(s['next_run']))}")
        if s["running"]:
            lines.append(f"- 実行中: @{s['current'] or '…'}（{s['progress'][0]}/{s['progress'][1]}）")
    elif not paused and last:
        lines.append(f"- 次回の巡回（目安）: {_local_dt(int(last) + interval * 60)}（ダッシュボード起動中のみ）")
    lines.append(f"- 保存 {st['total']}件 · 未読 {st['unread']}件 · スター {st['starred']}件 · 24時間の新着 {st['today']}件")
    errs = [a for a in store.accounts() if a["last_error"]]
    if errs:
        lines += ["", "## エラー中のアカウント", ""]
        lines += [f"- @{a['handle']}: {a['last_error']}（連続{a['fail_count']}回）" for a in errs]
    runs = store.runs(8)
    if runs:
        lines += ["", "## 最近の巡回", "", "| 開始 | きっかけ | 成功 | 失敗 | 新着 |", "|---|---|---:|---:|---:|"]
        lab = {"schedule": "定期", "manual": "手動", "cli": "CLI", "mcp": "AI"}
        lines += [f"| {_local_dt(r['started_at'])} | {lab.get(r['trigger'], r['trigger'])} | {r['ok']} | {r['failed']} | {r['new_posts']} |"
                  for r in runs]
    return "\n".join(lines) + "\n"


class MCPError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class MCPServer:
    """Model Context Protocol（JSON-RPC 2.0）。ダッシュボードの全機能を AI から使える。stdio と HTTP(/mcp)。"""

    INSTRUCTIONS = ("xwatch はユーザーが追跡している X(Twitter) アカウントの最新投稿をローカルに保存しています。"
                    "最新ニュースを知りたいときは get_new_posts（前回から増えた分）か get_latest_posts を使ってください。"
                    "アカウントの追加・削除、巡回設定、既読・スターなどダッシュボードと同じ操作もできます。"
                    "削除や設定変更はユーザーに頼まれたときだけ行ってください。"
                    "結果は Markdown です。投稿は各アカウントの直近数件で、完全な履歴ではありません。")

    def __init__(self, store: "Store", sched: "Scheduler | None" = None):
        self.store, self.sched = store, sched

    # ---- tool / resource 定義 ----
    def tools(self) -> list[dict]:
        H = {"type": "string", "description": "アカウント名（@なし可）。省略で全アカウント"}
        H1 = {"type": "string", "description": "アカウント名（@なし可）。プロフィールURLも可"}
        LIM = lambda d: {"type": "integer", "minimum": 1, "maximum": MD_MAX_POSTS, "default": d}
        ID = {"type": "string", "pattern": "^[0-9]+$", "description": "投稿ID（Markdown の「ID:」）"}

        def tool(name, title, desc, props=None, required=None, ro=True, destructive=False, world=False):
            schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
            if required:
                schema["required"] = required
            return {"name": name, "title": title, "description": desc, "inputSchema": schema,
                    "annotations": {"readOnlyHint": ro, "destructiveHint": destructive, "openWorldHint": world}}

        return [
            # 読む
            tool("get_new_posts", "新着投稿（前回確認以降）",
                 "前回このツールを呼んでから新しく保存された投稿を Markdown で返し、AI用の既読位置を進めます。"
                 "初回は直近24時間に取得した分。件数が多いときは続きを繰り返し呼べます。ダッシュボードの未読には影響しません。",
                 {"handle": H, "limit": LIM(50), "peek": {"type": "boolean", "default": False,
                                                          "description": "true なら既読位置を進めない"}}, ro=False),
            tool("get_latest_posts", "最新投稿",
                 "保存済みの投稿を新しい順に Markdown で返します。未読・スターで絞り込めます。",
                 {"handle": H, "hours": {"type": "number", "minimum": 0, "default": 24,
                                         "description": "直近何時間分か。0 で期間指定なし"},
                  "limit": LIM(30), "unread_only": {"type": "boolean", "default": False},
                  "starred_only": {"type": "boolean", "default": False}}),
            tool("search_posts", "投稿を検索", "保存済み投稿の本文を部分一致で検索し、Markdown で返します。",
                 {"query": {"type": "string", "minLength": 1}, "handle": H, "limit": LIM(20)}, ["query"]),
            tool("get_post", "投稿を1件取得", "投稿IDを指定して1件を Markdown で返します。", {"id": ID}, ["id"]),
            tool("list_accounts", "追跡アカウント一覧",
                 "追跡中のアカウント、保存件数、未読、最終取得時刻、エラー状態を Markdown 表で返します。"),
            tool("get_status", "巡回の状態",
                 "巡回間隔・自動巡回のオン/オフ・次回予定・実行中か・投稿数の統計・最近の巡回ログ・エラーを返します。"),
            # 操作する
            tool("refresh", "今すぐ取得",
                 "X から最新投稿の取得を開始します（非同期）。数十秒後に get_new_posts か get_status で確認してください。",
                 {"handle": H}, ro=False, world=True),
            tool("add_account", "アカウントを追加",
                 f"X のアカウントを追跡対象に追加し、すぐ最初の取得を始めます（最大{MAX_ACCOUNTS}件）。",
                 {"handle": H1}, ["handle"], ro=False, world=True),
            tool("remove_account", "アカウントを削除",
                 "追跡対象から外します。delete_posts=true なら保存済み投稿も消します（元に戻せません）。",
                 {"handle": H1, "delete_posts": {"type": "boolean", "default": False}}, ["handle"],
                 ro=False, destructive=True),
            tool("set_account_enabled", "アカウントの一時停止/再開",
                 "enabled=false で定期巡回から外します（投稿は残ります）。",
                 {"handle": H1, "enabled": {"type": "boolean"}}, ["handle", "enabled"], ro=False),
            tool("update_settings", "巡回設定を変更",
                 f"巡回間隔（{MIN_INTERVAL_MIN}〜{MAX_INTERVAL_MIN}分）や自動巡回のオン/オフを変更します。",
                 {"interval_min": {"type": "integer", "minimum": MIN_INTERVAL_MIN, "maximum": MAX_INTERVAL_MIN},
                  "auto_fetch": {"type": "boolean", "description": "false で自動巡回を止める"}}, ro=False),
            tool("mark_read", "既読にする",
                 "ダッシュボードの未読を既読にします。ids か handle のどちらか、または all=true を指定。",
                 {"ids": {"type": "array", "items": ID, "maxItems": 500}, "handle": H1,
                  "all": {"type": "boolean", "default": False}}, ro=False),
            tool("star_post", "スターを付ける/外す", "投稿にスターを付けます（on=false で外す）。",
                 {"id": ID, "on": {"type": "boolean", "default": True}}, ["id"], ro=False),
        ]

    def resources(self) -> list[dict]:
        r = [{"uri": "xwatch://feed/latest.md", "name": "latest", "title": "全アカウントの最新投稿（24時間）",
              "mimeType": "text/markdown"},
             {"uri": "xwatch://accounts.md", "name": "accounts", "title": "追跡アカウント一覧", "mimeType": "text/markdown"}]
        for a in self.store.accounts():
            r.append({"uri": f"xwatch://feed/{a['handle']}.md", "name": a["handle"],
                      "title": f"@{a['handle']} の最新投稿", "mimeType": "text/markdown"})
        return r

    # ---- 実装 ----
    def _refresh(self, handle: str | None) -> None:
        if self.sched is not None:  # ダッシュボード起動中: 本体の巡回に任せる
            self.sched.trigger(handle)
            return
        # stdio 単体: このプロセスで1回だけ取得（同時に1つまで）
        if getattr(self, "_bg", None) is not None and self._bg.is_alive():
            raise ValueError("前回の取得がまだ実行中です。少し待ってから get_status で確認してください")
        runner = Scheduler(self.store)
        self._bg = threading.Thread(target=runner.cycle, args=("mcp", [handle] if handle else None), daemon=True)
        self._bg.start()

    def _handle(self, raw) -> str | None:
        if raw in (None, ""):
            return None
        h = normalize_handle(str(raw))
        for a in self.store.accounts():
            if a["handle"].lower() == h.lower():
                return a["handle"]
        raise ValueError(f"@{h} は追跡していません。list_accounts で一覧を確認できます")

    def call_tool(self, name: str, args: dict) -> str:
        args = args or {}
        if name == "list_accounts":
            return accounts_markdown(self.store, self.sched)
        if name == "get_latest_posts":
            hours = float(args.get("hours", 24) or 0)
            return build_feed_markdown(self.store, handle=self._handle(args.get("handle")),
                                       hours=hours or None, limit=int(args.get("limit", 30)),
                                       unread_only=bool(args.get("unread_only")),
                                       starred_only=bool(args.get("starred_only")))[0]
        if name == "search_posts":
            q = str(args.get("query") or "").strip()
            if not q:
                raise ValueError("query を指定してください")
            return build_feed_markdown(self.store, handle=self._handle(args.get("handle")), hours=None,
                                       query=q, limit=int(args.get("limit", 20)), title=f"検索結果: {q}")[0]
        if name == "get_new_posts":
            h = self._handle(args.get("handle"))
            limit = int(args.get("limit", 50))
            if not 1 <= limit <= MD_MAX_POSTS:
                raise ValueError(f"limit は1〜{MD_MAX_POSTS}で指定してください")
            key = "mcp_cursor:" + (h.lower() if h else "*")
            cursor = self.store.get(key)
            lo, hi = self.store.rowid_bounds(int(time.time()) - 86400)
            first = cursor is None
            after = lo if first else min(int(cursor), hi)  # 投稿削除で番号が戻った場合に備える
            posts, last, remaining = self.store.new_posts(after, h, limit)
            if not args.get("peek"):
                self.store.set(key, last if posts else max(after, hi if not h else after))
            footer = (f"まだ {remaining} 件あります。もう一度 get_new_posts を呼ぶと続きを返します。"
                      if remaining and not args.get("peek") else
                      f"ほかに {remaining} 件あります（peek のため既読位置は進めていません）。" if remaining else None)
            return build_feed_markdown(
                self.store, handle=h, hours=None, limit=MD_MAX_POSTS, posts=posts,
                title="xwatch 新着投稿",
                cond_note="初回: 直近24時間に取得した分" if first else "前回の確認以降",
                footer=footer)[0]
        if name == "get_post":
            pid = str(args.get("id") or "")
            if not pid.isdigit():
                raise ValueError("id は数字で指定してください")
            p = self.store.post(pid)
            if not p:
                raise ValueError(f"ID {pid} の投稿は保存されていません")
            return post_to_markdown(p, level=2) + "\n"
        if name == "get_status":
            return status_markdown(self.store, self.sched)
        if name == "refresh":
            h = self._handle(args.get("handle"))
            self._refresh(h)
            return f"取得を開始しました（{'@' + h if h else '有効な全アカウント'}）。数十秒後に get_new_posts で確認してください。\n"
        if name == "add_account":
            h = normalize_handle(str(args.get("handle") or ""))
            for a in self.store.accounts():
                if a["handle"].lower() == h.lower():
                    raise ValueError(f"@{a['handle']} はすでに登録されています")
            self.store.add_account(h)
            self._refresh(h)
            return f"@{h} を追加し、最初の取得を開始しました。数十秒後に get_latest_posts(handle=\"{h}\") で確認できます。\n"
        if name == "remove_account":
            h = self._handle(args.get("handle") or "-")
            purge = bool(args.get("delete_posts"))
            self.store.remove_account(h, purge)
            return f"@{h} を追跡対象から外しました" + ("（保存済み投稿も削除）。\n" if purge else "（保存済み投稿は残しています）。\n")
        if name == "set_account_enabled":
            h = self._handle(args.get("handle") or "-")
            if not isinstance(args.get("enabled"), bool):
                raise ValueError("enabled を true/false で指定してください")
            self.store.set_enabled(h, args["enabled"])
            return f"@{h} の定期巡回を{'再開' if args['enabled'] else '一時停止'}しました。\n"
        if name == "update_settings":
            done = []
            if args.get("interval_min") is not None:
                v = int(args["interval_min"])
                if not MIN_INTERVAL_MIN <= v <= MAX_INTERVAL_MIN:
                    raise ValueError(f"interval_min は{MIN_INTERVAL_MIN}〜{MAX_INTERVAL_MIN}で指定してください")
                self.store.set("interval_min", v)
                done.append(f"巡回間隔を{v}分に")
            if args.get("auto_fetch") is not None:
                self.store.set("paused", not bool(args["auto_fetch"]))
                done.append("自動巡回を" + ("オン" if args["auto_fetch"] else "オフ") + "に")
            if not done:
                raise ValueError("interval_min か auto_fetch を指定してください")
            if self.sched is not None:
                self.sched.reconfigure()
            return "、".join(done) + "しました。\n\n" + status_markdown(self.store, self.sched)
        if name == "mark_read":
            ids = [str(i) for i in (args.get("ids") or []) if str(i).isdigit()][:500]
            if ids:
                n = self.store.mark_read(None, ids)
            elif args.get("handle"):
                n = self.store.mark_read(self._handle(args["handle"]), None)
            elif args.get("all"):
                n = self.store.mark_read(None, None)
            else:
                raise ValueError("ids・handle・all=true のどれかを指定してください")
            return f"{n}件を既読にしました。\n"
        if name == "star_post":
            pid = str(args.get("id") or "")
            if not pid.isdigit() or not self.store.post(pid):
                raise ValueError(f"ID {pid} の投稿は保存されていません")
            on = bool(args.get("on", True))
            self.store.star(pid, on)
            return f"ID {pid} のスターを{'付けました' if on else '外しました'}。\n"
        raise MCPError(-32602, f"unknown tool: {name}")

    def read_resource(self, uri: str) -> str:
        if uri == "xwatch://accounts.md":
            return accounts_markdown(self.store, self.sched)
        m = re.fullmatch(r"xwatch://feed/([A-Za-z0-9_]{1,15}|latest)\.md", uri or "")
        if not m:
            raise MCPError(-32002, f"resource not found: {uri}")
        h = None if m.group(1) == "latest" else self._handle(m.group(1))
        return build_feed_markdown(self.store, handle=h, hours=24, limit=50)[0]

    # ---- JSON-RPC ----
    def dispatch(self, msg) -> dict | None:
        """1メッセージを処理。通知（id なし）なら None を返す。"""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None  # クライアントからの応答は無視
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        is_note = "id" not in msg
        try:
            result = self._method(method, params)
        except MCPError as e:
            if is_note:
                return None
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:  # 想定外
            log.exception("MCP %s", method)
            if is_note:
                return None
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return None if is_note else {"jsonrpc": "2.0", "id": mid, "result": result}

    def _method(self, method: str, params: dict):
        if method == "initialize":
            want = params.get("protocolVersion")
            return {"protocolVersion": want if want in MCP_PROTOCOLS else MCP_PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                    "serverInfo": {"name": "xwatch", "title": "xwatch（X 最新投稿）", "version": __version__},
                    "instructions": self.INSTRUCTIONS}
        if method.startswith("notifications/"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            name = params.get("name")
            if name not in {t["name"] for t in self.tools()}:
                raise MCPError(-32602, f"unknown tool: {name}")
            try:
                text = self.call_tool(name, params.get("arguments") or {})
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except (ValueError, TypeError) as e:  # 入力ミスはツールエラーとして AI に返す
                return {"content": [{"type": "text", "text": f"エラー: {e}"}], "isError": True}
        if method == "resources/list":
            return {"resources": self.resources()}
        if method == "resources/templates/list":
            return {"resourceTemplates": [{"uriTemplate": "xwatch://feed/{handle}.md", "name": "feed",
                                           "title": "アカウント別の最新投稿", "mimeType": "text/markdown"}]}
        if method == "resources/read":
            uri = params.get("uri", "")
            try:
                text = self.read_resource(uri)
            except ValueError as e:
                raise MCPError(-32002, str(e))
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}
        if method == "prompts/list":
            return {"prompts": []}
        raise MCPError(-32601, f"Method not found: {method}")

    def handle_payload(self, payload) -> list | dict | None:
        if isinstance(payload, list):
            if not payload:
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
            out = [r for r in (self.dispatch(m) for m in payload) if r is not None]
            return out or None
        return self.dispatch(payload)


def serve_mcp_stdio(store: "Store") -> int:
    """stdio トランスポート。1行1メッセージの JSON-RPC。stdout には JSON-RPC 以外を書かない。"""
    server = MCPServer(store)
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
    for raw in stdin:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            resp = server.handle_payload(payload)
        if resp is not None:
            data = json.dumps(resp, ensure_ascii=False) + "\n"
            out.write(data.encode("utf-8") if out is not sys.stdout else data)
            out.flush()
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# HTTP サーバー
# ──────────────────────────────────────────────────────────────────────────────
IMG_HOSTS = {"pbs.twimg.com", "abs.twimg.com"}
IMG_CACHE_DAYS = 45


class ImageCache:
    """pbs.twimg.com の画像をローカルにキャッシュして配信する（過去の投稿も画像付きで見返せる）。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}

    def prune(self) -> None:
        limit = time.time() - IMG_CACHE_DAYS * 86400
        for f in self.root.glob("*"):
            try:
                if f.stat().st_atime < limit and f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass

    def get(self, url: str) -> tuple[bytes, str]:
        u = urllib.parse.urlparse(url)
        if u.scheme != "https" or u.hostname not in IMG_HOSTS:
            raise ValueError("対応していない画像URLです")
        key = hashlib.sha1(url.encode()).hexdigest()
        path = self.root / key
        ctype_path = self.root / (key + ".t")
        if path.exists():
            ctype = ctype_path.read_text() if ctype_path.exists() else "image/jpeg"
            return path.read_bytes(), ctype
        with self._lock:
            ev = self._inflight.get(key)
            owner = ev is None
            if owner:
                ev = self._inflight[key] = threading.Event()
        if not owner:
            ev.wait(30)
            if path.exists():
                return path.read_bytes(), (ctype_path.read_text() if ctype_path.exists() else "image/jpeg")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read(15 * 1024 * 1024)
                ctype = r.headers.get_content_type() or "image/jpeg"
            if not ctype.startswith("image/"):
                raise ValueError("画像ではありません")
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            ctype_path.write_text(ctype)
            return data, ctype
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            ev.set()


def normalize_handle(raw: str) -> str:
    s = (raw or "").strip()
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip()
    if not HANDLE_RE.match(s):
        raise ValueError("アカウント名は英数字と _ の15文字以内です（例: @thsottiaux）")
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = f"xwatch/{__version__}"
    store: Store
    sched: Scheduler
    images: "ImageCache"
    mcp: "MCPServer"
    data_dir: Path = DEFAULT_DATA_DIR

    def log_message(self, fmt, *args):  # アクセスログは静かに
        log.debug("%s - %s", self.address_string(), fmt % args)

    # helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("リクエストが大きすぎます")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _same_origin(self) -> bool:
        # 他サイトから localhost の API を叩かれないよう Origin を確認
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc == host

    # routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            pwa_files = {
                "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                "/sw.js": ("sw.js", "text/javascript"),
                "/icon-192.png": ("icon-192.png", "image/png"),
                "/icon-512.png": ("icon-512.png", "image/png"),
            }
            if u.path in pwa_files:
                name, content_type = pwa_files[u.path]
                return self._send(200, (APP_DIR / name).read_bytes(), content_type)
            if u.path == "/api/state":
                since = int(qs.get("since", "0") or 0)
                return self._json({
                    "version": __version__,
                    "scheduler": self.sched.state(),
                    "accounts": self.store.accounts(),
                    "stats": self.store.stats(),
                    "events": [e for e in self.sched.events if e["seq"] > since],
                    "limits": {"min_interval": MIN_INTERVAL_MIN, "max_interval": MAX_INTERVAL_MIN,
                               "max_accounts": MAX_ACCOUNTS},
                    "now": time.time(),
                    "mcp": {"python": sys.executable or "python", "script": str(APP_DIR / "xwatch.py"),
                            "data": str(self.data_dir.resolve()),
                            "default_data": self.data_dir.resolve() == DEFAULT_DATA_DIR.resolve()},
                })
            if u.path in ("/feed.md", "/accounts.md"):
                if u.path == "/accounts.md":
                    md = accounts_markdown(self.store, self.sched)
                else:
                    try:
                        h = self.mcp._handle(qs.get("handle"))
                        hours = float(qs.get("hours", "24") or 0)
                        limit = int(qs.get("limit", "30"))
                        md = build_feed_markdown(self.store, handle=h, hours=hours or None, limit=limit,
                                                 unread_only=qs.get("unread") == "1",
                                                 query=(qs.get("q") or "").strip() or None)[0]
                    except ValueError as e:
                        return self._send(400, f"エラー: {e}\n".encode(), "text/markdown; charset=utf-8")
                return self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8")
            if u.path == "/mcp":
                return self._mcp_not_allowed()
            if u.path == "/api/posts":
                limit = max(1, min(200, int(qs.get("limit", "40"))))
                return self._json({"posts": self.store.posts(
                    qs.get("handle") or None, (qs.get("q") or "").strip() or None,
                    qs.get("unread") == "1", qs.get("starred") == "1", qs.get("before") or None, limit)})
            if u.path == "/api/runs":
                return self._json({"runs": self.store.runs()})
            if u.path == "/api/export":
                data = json.dumps({"accounts": self.store.accounts(),
                                   "posts": self.store.posts(None, None, False, False, None, 100000)},
                                  ensure_ascii=False, indent=1).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="xwatch-{datetime.now():%Y%m%d-%H%M}.json"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/img":
                try:
                    data, ctype = self.images.get(qs.get("u", ""))
                except ValueError as e:
                    return self._error(str(e), 400)
                except (urllib.error.URLError, OSError, TimeoutError):
                    return self._error("画像を取得できませんでした", 502)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=2592000, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            self._error("not found", 404)
        except Exception as e:
            log.exception("GET %s", self.path)
            self._error(str(e), 500)

    def _mcp_not_allowed(self):
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        if urllib.parse.urlparse(self.path).path == "/mcp":
            return self._mcp_not_allowed()
        self._error("not found", 404)

    def _post_mcp(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1024 * 1024:
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "too large"}}, 413)
        try:
            payload = json.loads(self.rfile.read(n) or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        resp = self.mcp.handle_payload(payload)
        if resp is None:  # 通知・応答のみ
            return self._send(202, b"", "application/json")
        return self._json(resp)

    def do_POST(self):
        if not self._same_origin():
            return self._error("forbidden", 403)
        u = urllib.parse.urlparse(self.path)
        if u.path == "/mcp":
            try:
                return self._post_mcp()
            except Exception as e:
                log.exception("MCP POST")
                return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}, 500)
        try:
            body = self._body()
            if u.path == "/api/accounts":
                handle = normalize_handle(body.get("handle", ""))
                if not self.store.add_account(handle):
                    return self._error(f"@{handle} はすでに登録されています", 409)
                self.sched.trigger(handle)
                return self._json({"ok": True, "handle": handle})
            if u.path == "/api/accounts/remove":
                self.store.remove_account(normalize_handle(body.get("handle", "")), bool(body.get("purge")))
                return self._json({"ok": True})
            if u.path == "/api/accounts/toggle":
                self.store.set_enabled(normalize_handle(body.get("handle", "")), bool(body.get("enabled")))
                return self._json({"ok": True})
            if u.path == "/api/settings":
                if "interval_min" in body:
                    v = int(body["interval_min"])
                    if not MIN_INTERVAL_MIN <= v <= MAX_INTERVAL_MIN:
                        return self._error(f"間隔は{MIN_INTERVAL_MIN}〜{MAX_INTERVAL_MIN}分で指定してください")
                    self.store.set("interval_min", v)
                if "paused" in body:
                    self.store.set("paused", bool(body["paused"]))
                self.sched.reconfigure()
                return self._json({"ok": True, "scheduler": self.sched.state()})
            if u.path == "/api/fetch":
                h = body.get("handle")
                self.sched.trigger(normalize_handle(h) if h else None)
                return self._json({"ok": True})
            if u.path == "/api/read":
                ids = [str(i) for i in body.get("ids") or [] if str(i).isdigit()][:500]
                h = body.get("handle")
                n = self.store.mark_read(normalize_handle(h) if h else None, ids or None)
                return self._json({"ok": True, "count": n})
            if u.path == "/api/star":
                pid = str(body.get("id", ""))
                if not pid.isdigit():
                    return self._error("invalid id")
                self.store.star(pid, bool(body.get("on")))
                return self._json({"ok": True})
            self._error("not found", 404)
        except ValueError as e:
            self._error(str(e))
        except Exception as e:
            log.exception("POST %s", self.path)
            self._error(str(e), 500)


# ──────────────────────────────────────────────────────────────────────────────
# エントリポイント
# ──────────────────────────────────────────────────────────────────────────────
def run_once(store: Store) -> int:
    accounts = [a for a in store.accounts() if a["enabled"]]
    if not accounts:
        print("登録アカウントがありません。先に `python xwatch.py` でダッシュボードから追加してください。")
        return 1
    sched = Scheduler(store)
    sched.cycle("cli", None)
    for a in store.accounts():
        status = "OK" if not a["last_error"] else f"NG ({a['last_error']})"
        print(f"@{a['handle']:<16} 未読 {a['unread']:>3} / 保存 {a['total']:>4}  {status}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="X の公開アカウントを定期巡回するローカルダッシュボード")
    ap.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス（既定: 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8765, help="ポート番号（既定: 8765）")
    ap.add_argument("--data", default=str(DEFAULT_DATA_DIR), help="データ保存フォルダ")
    ap.add_argument("--no-browser", action="store_true", help="起動時にブラウザを開かない")
    ap.add_argument("--once", action="store_true", help="1回だけ巡回して終了")
    ap.add_argument("--add", nargs="*", metavar="HANDLE", help="アカウントを追加（例: --add thsottiaux OpenAI）")
    ap.add_argument("--interval", type=int, help=f"巡回間隔（分, {MIN_INTERVAL_MIN}〜{MAX_INTERVAL_MIN}）")
    ap.add_argument("--mcp", action="store_true",
                    help="AI用 MCP サーバーとして stdio で起動（Claude Desktop / Codex などの設定から使う）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers: list[logging.Handler] = [RotatingFileHandler(data_dir / ("mcp.log" if args.mcp else "xwatch.log"), maxBytes=1_000_000,
                                                           backupCount=3, encoding="utf-8")]
    if args.mcp:  # stdout は JSON-RPC 専用。ログはファイルと stderr へ
        if sys.stderr is not None:
            handlers.append(logging.StreamHandler(sys.stderr))
    elif sys.stdout is not None:  # pythonw（ウィンドウなし起動）では stdout が無い
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%m-%d %H:%M:%S",
        handlers=handlers,
    )
    store = Store(data_dir / "xwatch.db")
    if args.mcp:
        log.info("MCP(stdio) 起動 / データ: %s", data_dir)
        return serve_mcp_stdio(store)

    for raw in args.add or []:
        try:
            h = normalize_handle(raw)
            print(("追加: @" if store.add_account(h) else "登録済み: @") + h)
        except ValueError as e:
            print(f"{raw}: {e}")
    if args.interval is not None:
        if not MIN_INTERVAL_MIN <= args.interval <= MAX_INTERVAL_MIN:
            print(f"--interval は {MIN_INTERVAL_MIN}〜{MAX_INTERVAL_MIN} 分で指定してください")
            return 2
        store.set("interval_min", args.interval)

    if args.once:
        return run_once(store)

    sched = Scheduler(store)
    Handler.store, Handler.sched = store, sched
    Handler.images = ImageCache(data_dir / "cache")
    Handler.mcp = MCPServer(store, sched)
    Handler.data_dir = data_dir
    threading.Thread(target=Handler.images.prune, daemon=True).start()
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"ポート {args.port} を使えませんでした（{e}）。--port で別の番号を指定してください。")
        return 2
    httpd.daemon_threads = True
    sched.start()

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    log.info("xwatch %s 起動: %s  （Ctrl+C で終了）", __version__, url)
    log.info("AI連携: MCP %smcp ・ Markdown %sfeed.md", url, url)
    log.info("巡回間隔 %d分 / 登録 %d件 / データ: %s", sched.interval_min, len(store.accounts()), data_dir)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("終了します")
    finally:
        sched.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
