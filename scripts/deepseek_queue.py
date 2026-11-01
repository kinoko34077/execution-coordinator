"""Persist DeepSeek pending requests in the existing public Issue #162."""
from __future__ import annotations

import hashlib
import json
import os
import re

from scripts import deepseek_issue_gateway as gw

URL = f"{gw.HUB}/repos/{gw.OWNER}/execution-coordinator/issues/{gw.ISSUE_NUMBER}/comments"
QUEUE = re.compile(r"<!-- dsq:v1 id=(\d+) sha=([0-9a-f]{64}) -->")
CANCEL = re.compile(r"<!-- dsc:v1 id=(\d+) -->")
START = re.compile(r"^### DeepSeek実行受付\s*\n\s*GitHub comment #(\d+)", re.M)
FINISH = re.compile(r"<!-- dsd:v1 id=(\d+) -->")
CANCEL_CMD = re.compile(r"^/deepseek cancel ([1-9]\d{0,18})$")
MAX_PAGES = 10


def records(token: str) -> list[dict]:
    all_rows = []
    for page in range(1, MAX_PAGES + 1):
        batch = gw.request_json(f"{URL}?per_page=100&page={page}", token=token)
        if not isinstance(batch, list) or any(not isinstance(x, dict) for x in batch):
            raise gw.Refused("invalid queue history")
        all_rows.extend(batch)
        if len(batch) < 100:
            return all_rows
    raise gw.Refused("queue history requires paging beyond configured limit")


def parsed(row: dict) -> tuple[str, str] | None:
    user = row.get("user") or {}
    if user.get("login") != gw.OWNER or user.get("id") != 79015263:
        return None
    body = row.get("body")
    if not isinstance(body, str) or len(body) > gw.MAX_INPUT + 100:
        return None
    m = gw.COMMAND.fullmatch(body.strip())
    if not m:
        return None
    mode, arg = m.group(1), (m.group(2) or "").strip()
    if mode == "ask" and 0 < len(arg) <= gw.MAX_INPUT:
        return (mode, arg)
    if mode == "review" and gw.PR_REF.fullmatch(arg):
        return (mode, arg)
    return None


def digest(row: dict) -> str:
    return hashlib.sha256(row["body"].strip().encode("utf-8")).hexdigest()


def state(rows: list[dict], source: int) -> tuple[str, str | None]:
    queued = None
    cancel = started = finished = False
    for row in rows:
        if (row.get("user") or {}).get("login") != "github-actions[bot]":
            continue
        body = row.get("body") or ""
        if not isinstance(body, str):
            raise gw.Refused("invalid queue marker")
        q, c, d, a = QUEUE.search(body), CANCEL.search(body), FINISH.search(body), START.search(body)
        if q and int(q.group(1)) == source:
            queued = q.group(2)
        if c and int(c.group(1)) == source:
            cancel = True
        if d and int(d.group(1)) == source:
            finished = True
        if a and int(a.group(1)) == source:
            started = True
    if started:
        return ("started", queued)
    if cancel:
        return ("cancelled", queued)
    if finished:
        return ("finished", queued)
    if queued:
        return ("queued", queued)
    return ("new", None)


def cancellation_target(event: dict, actor: str) -> int | None:
    comment = event.get("comment") or {}
    body = comment.get("body")
    if not isinstance(body, str):
        return None
    m = CANCEL_CMD.fullmatch(body.strip())
    if not m:
        return None
    # Reuse the owning account numeric-ID and sender admission.
    virtual = {**event, "comment": {**comment, "body": "/deepseek status"}}
    if gw.admission(virtual, actor) != ("status", ""):
        return None
    return int(m.group(1))


def cancel_reply(source: int, rows: list[dict]) -> str:
    original = next((x for x in rows if x.get("id") == source), None)
    if original is None or parsed(original) is None:
        return "### DeepSeek取り下げ不可\n\n該当する依頼はありません。"
    current, _ = state(rows, source)
    if current == "started":
        return f"### DeepSeek取り下げ不可\n\n依頼 #{source} は実行開始済みです。"
    if current == "finished":
        return f"### DeepSeek取り下げ不可\n\n依頼 #{source} は完了済みです。"
    if current == "cancelled":
        return f"### DeepSeek取り下げ済み\n\n依頼 #{source} は既に取消済みです。"
    return (f"<!-- dsc:v1 id={source} -->\n### DeepSeek取り下げ完了\n\n"
            f"依頼 #{source} を取り下げました。")


def queue_reply(original: dict, rows: list[dict]) -> str:
    source = original.get("id")
    if type(source) is not int or source <= 0 or parsed(original) is None:
        raise gw.Refused("invalid request origin")
    current, _ = state(rows, source)
    if current != "new":
        return f"### DeepSeek重複受付拒否\n\n依頼 #{source} は {current} です。"
    pending = sum(state(rows, row["id"])[0] == "queued"
                  for row in rows if type(row.get("id")) is int and parsed(row))
    if pending >= 100:
        raise gw.Refused("pending queue is full")
    return (f"<!-- dsq:v1 id={source} sha={digest(original)} -->\n"
            f"### DeepSeek自動延期\n\n依頼 #{source} を保存しました。"
            " オフピーク時にGitHub Actionsが順次実行します。\n\n"
            f"取り下げ: `/deepseek cancel {source}`")
