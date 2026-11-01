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
START = re.compile(r"\A### DeepSeek実行受付\s*\n\s*GitHub comment #(\d+)")
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
    if finished:
        return ("finished", queued)
    if started:
        return ("started", queued)
    if cancel:
        return ("cancelled", queued)
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


def run_one(original: dict, token: str, key: str, rows: list[dict]) -> str | None:
    source = original.get("id")
    info = parsed(original)
    if type(source) is not int or info is None:
        raise gw.Refused("invalid request")
    current, saved = state(rows, source)
    if current in ("started", "cancelled", "finished"):
        return None
    if current == "queued" and saved != digest(original):
        return (f"<!-- dsd:v1 id={source} -->\n### DeepSeek依頼無効\n\n"
                f"依頼 #{source} は予約後に編集されました。")
    if not gw.gate_enabled(os.environ.get("DEEPSEEK_ENABLED", "true")):
        return None
    if not gw.offpeak_allowed() or not key:
        return None
    mode, arg = info
    if mode == "review":
        try:
            prompt, head, label = gw.review_prompt(arg, token)
        except gw.Refused as exc:
            return (f"<!-- dsd:v1 id={source} -->\n### DeepSeekレビュー不可\n\n{exc}")
    else:
        prompt, head, label = arg, None, None
    gw.admission_budget(token, source)
    # Check off-peak and OFF again immediately before the irreversible reservation.
    # The DeepSeek transport repeats this check before its HTTPS request.
    gw.policy_check(os.environ.get("DEEPSEEK_ENABLED", "true"))
    # Persist the one-use reservation before any model request; a rerun never re-bills.
    gw.publish(f"### DeepSeek実行受付\n\nGitHub comment #{source}\n\n"
               "この依頼は再実行しません。", token)
    try:
        text, usage = gw.deepseek_complete(prompt, key, review=(mode == "review"))
        if head is not None:
            if not gw.still_current(arg, head, token):
                raise gw.Refused("PR head changed; review output discarded")
            output = (f"### DeepSeek参考レビュー — {label}\nReviewed head: `{head}`"
                      f"\n\n{text}\n\n正式GitHub Reviewではありません。")
        else:
            output = f"### DeepSeek応答（参考情報）\n\n{text}"
        return (f"<!-- dsd:v1 id={source} -->\n{output}\n\n"
                f"依頼 #{source}; 使用トークン: {usage}")
    except gw.Refused as exc:
        return (f"<!-- dsd:v1 id={source} -->\n### DeepSeek実行失敗\n\n"
                f"依頼 #{source}: {exc}。自動再試行はしません。")


def scheduled_once(token: str, key: str, rows: list[dict]) -> str | None:
    if not key or not gw.gate_enabled(os.environ.get("DEEPSEEK_ENABLED", "true")):
        return None
    if not gw.offpeak_allowed():
        return None
    candidates = sorted(
        (r for r in rows if type(r.get("id")) is int and parsed(r)),
        key=lambda r: r["id"],
    )
    for row in candidates:
        if state(rows, row["id"])[0] == "queued":
            return run_one(row, token, key, rows)
    return None


def issue_event(event: dict, actor: str, token: str, key: str) -> str | None:
    cancellation = cancellation_target(event, actor)
    accepted = gw.admission(event, actor)
    if cancellation is None and accepted is None:
        return None
    if accepted and accepted[0] == "status":
        return gw.run(event, actor, token, key)
    rows = records(token)
    if cancellation is not None:
        return cancel_reply(cancellation, rows)
    if accepted is None or accepted[0] not in ("ask", "review"):
        return None
    # OFF stops new admissions, while preserving requests previously queued.
    if not gw.gate_enabled(os.environ.get("DEEPSEEK_ENABLED", "true")):
        return "### DeepSeek停止中\n\n新規受付は停止中です。既存の予約は保持します。"
    if not key:
        return "### DeepSeek未設定\n\nDEEPSEEK_API_KEY を登録してから依頼してください。"
    if not gw.offpeak_allowed():
        return queue_reply(event["comment"], rows)
    return run_one(event["comment"], token, key, rows)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GitHub token missing")
        return 1
    try:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
            if not key or not gw.gate_enabled(os.environ.get("DEEPSEEK_ENABLED", "true")):
                return 0
            if not gw.offpeak_allowed():
                return 0
            reply = scheduled_once(token, key, records(token))
        else:
            with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
                event = json.load(stream)
            reply = issue_event(event, os.environ.get("GITHUB_ACTOR", ""), token, key)
        if reply is not None:
            gw.publish(reply, token)
        print("DeepSeek queue cycle complete; no private content logged.")
        return 0
    except gw.Refused as exc:
        print("DeepSeek queue held: " + str(exc))
        return 0
    except Exception:
        print("DeepSeek queue error; no input, output or secret logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
