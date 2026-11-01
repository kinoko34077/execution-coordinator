# DeepSeek GitHub Issue gateway — cloud-only, off-peak

Owner: [Issue #162](https://github.com/kinoko34077/execution-coordinator/issues/162).
This gateway invokes the DeepSeek API from **GitHub-hosted Actions**, never from the local `dev_agent` or Windows computer. Only user-initiated, single-request commands on the fixed Issue are accepted. No cron, auto-merge, model shell tools, or repository mutation by DeepSeek.

## One-time GitHub setup

1. Open [execution-coordinator Actions secrets](https://github.com/kinoko34077/execution-coordinator/settings/secrets/actions). Create repository secret **`DEEPSEEK_API_KEY`** with the API key from your existing DeepSeek account. Never paste the key into Issue comments or chat. Do not set a secret at `dev_agent`; it is not involved in this runtime.
2. **Optional on/off switch:** [execution-coordinator Actions variables](https://github.com/kinoko34077/execution-coordinator/settings/variables/actions) → create repository variable `DEEPSEEK_ENABLED`. `false` = stop future model requests; `true` = permit. When missing, it defaults to `true`, so **Secret registration alone makes explicit commands eligible**. Turning OFF does not interrupt a request already in flight.
3. **Optional daily cap:** Variable `DEEPSEEK_DAILY_LIMIT` (integer 1..100), default `20` attempted requests per UTC date. Each request reserves a comment before the API call. Requests with failed or incomplete model output still count; same-comment replay is rejected. Neither variable is a secret.
4. All DeepSeek requests are user-invoked through the fixed [command Issue #162](https://github.com/kinoko34077/execution-coordinator/issues/162). The initiating GitHub comment must be authored by account ID `79015263`; a connector bot is a transport, not an independent spending principal.

## Commands

Post a new single-line Issue comment:

- `/deepseek status`: reports whether the secret is present, whether the switch is ON, and whether this instant is off-peak. **Does not call DeepSeek**.
- `/deepseek ask 日本語で短い質問`: sends one bounded prompt, posts the response as a comment.
- `/deepseek review kinoko34077/execution-coordinator#163`: retrieves a **public** same-owner PR and a bounded patch, pins its head SHA, obtains an advisory review, checks that the PR head is still current, then posts the result in Issue #162. This is **not** a native GitHub Formal Review, required review approval, code change, CI result, or merge permission.

Only exact commands are accepted; no background jobs or automatic review scheduling. The ChatGPT GitHub connection can submit such comments on the user's behalf when authorized.

## Mandatory peak block

DeepSeek's [official API pricing](https://api-docs.deepseek.com/quick_start/pricing/) identifies peak hours as:

- Monday–Friday **01:00–04:00 UTC** and **06:00–10:00 UTC**.
- In Japan (UTC+9): Monday–Friday **10:00–13:00 JST** and **15:00–19:00 JST**.
- Officially, Chinese public holidays are excluded; this initial gateway **conservatively blocks the same weekday hours even on Chinese public holidays**, rather than risk a false off-peak classification.
- The gateway also blocks the **2 minutes before** each peak window starts (09:58–10:00 and 14:58–15:00 JST). Weekends are permitted throughout.
- The UTC gate is checked at request admission **and again immediately before the model HTTP call**. There is no delayed queue: a peak-period request is refused; submit a fresh command during off-peak hours.

## Technical and privacy boundaries

- Workflow: `.github/workflows/deepseek-transport-preflight.yml` (GitHub UI name: `DeepSeek GitHub Issue gateway`), must exist on `main` to receive issue_comment events.
- Implementation: `scripts/deepseek_issue_gateway.py`; Python standard library, `https://api.deepseek.com/chat/completions`, `deepseek-flash` and non-thinking mode (`reasoning_effort=none`). No third-party packages or local checkout.
- User questions and returned model text appear in the repository's **public Issue**. Do not submit secrets, private repository data, or unpublished sensitive documents. Review mode refuses private repositories and oversized/non-text diffs.
- Keys are supplied only through Actions secrets; error bodies, keys and raw prompts are not written to Actions logs. Credential-bearing redirects are refused.
- Server-side DeepSeek usage is metered against the user's configured provider account; this gateway cannot prove account-level prepayment/overage settings. Daily cap and ON/OFF prevent accidental scheduled or replay usage.
- The first live model call requires the user to register the secret; before that, `/deepseek status` is an uncharged end-to-end transport test.
- In case of failure after the request reservation, **do not rerun the same comment**: create a new comment after diagnosis. The existing request is not automatically retried.

## Evidence and verification

Owning Issue #162, implementing PR #163. The offline tests in `tests/test_deepseek_issue_gateway.py` validate event ownership, UTC peak boundaries, Japan timezone conversion, OFF switch, zero HTTP calls while blocked, public PR pinning, and response validation. GitHub Actions `verify` runs the suite; full live DeepSeek response remains untested until the owner supplies the key.
