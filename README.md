# Weibo Publisher

A local, single-account scheduled Weibo publisher for Windows.

## What it does

- Logs in once through a dedicated Playwright browser profile.
- Stores text-and-image jobs in a local SQLite database.
- Keeps new jobs as drafts until they are explicitly confirmed.
- Prepares an approved post three minutes before its scheduled time.
- Publishes unattended while the local runner is active.
- Records success, failure, and uncertain outcomes without automatically
  retrying an uncertain submission.

## Requirements

- Windows.
- Python 3.11 or newer.
- Microsoft Edge or Playwright Chromium.
- A valid Weibo login session.
- The computer must be powered on, connected to the internet, and running the
  scheduler at publish time.

## Install

Run these commands from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

The default configuration uses Microsoft Edge. If Edge is unavailable, set
`browser_channel = ""` in the `[app]` section of `config.toml` to use the
Playwright-installed Chromium. Start from `config.example.toml` when you need
to create a local configuration.

## First login

```powershell
python -m weibo_publisher login
```

Scan the QR code in the opened browser and complete the login. The session is
stored in the dedicated browser profile. The command never asks for or stores
your Weibo password.

You can check the local environment and browser session with:

```powershell
python -m weibo_publisher doctor
```

## Create a draft

```powershell
python -m weibo_publisher add --text "可爱" --image "D:\path\image.jpg" --at "2026-11-07T17:30:00+08:00"
```

Repeat `--image` to attach more than one image. The publish time must be an ISO
8601 timestamp with a timezone. `add` creates a draft and does not publish it.

## Show and confirm

```powershell
python -m weibo_publisher show JOB_ID
python -m weibo_publisher confirm JOB_ID
```

Review the text, images, time, and visibility before confirming. Real
publishing requires explicit task confirmation; only approved jobs are
scheduled. Confirmation also requires enough time for the three-minute
preparation window.

## Preview

```powershell
python -m weibo_publisher preview JOB_ID
```

Preview fills a draft or approved job in the Weibo composer without clicking
Publish. It saves a screenshot under `runtime/artifacts/JOB_ID/preview.png` for
inspection.

## List jobs

```powershell
python -m weibo_publisher list
python -m weibo_publisher list --status approved
```

## Run the scheduler

```powershell
python -m weibo_publisher run
```

Keep the runner active until the jobs you want to publish have reached a final
state. Press `Ctrl+C` to stop it. The runner rescans the database while it is
active.

## Check a job

```powershell
python -m weibo_publisher status JOB_ID
```

`status` shows the job state, artifact directory, and publish attempts with
their outcomes. `show` displays the job's content, schedule, and result URL.

## Limits and publication behavior

The default `daily_limit` of 100 is a local technical ceiling, not a guarantee
that Weibo will accept 100 posts. Weibo may rate-limit, review, reject, or
challenge an account regardless of local limits. The default minimum gap
between posts is 240 seconds, and `allow_over_limit` is disabled.

The runner prepares a post three minutes before its scheduled time. A post more
than 30 seconds late is marked missed. If the publish outcome cannot be
determined, the job is recorded as uncertain and is not retried automatically,
which avoids accidental duplicate posts.

## Known limitations

- The computer must stay powered on and awake, and the local `run` command must
  remain active for scheduled publications.
- The login session may expire and require another QR-code login.
- Weibo page structure and network behavior may change without notice.
- A local limit does not guarantee acceptance by Weibo.
- Live publishing against the current Weibo web client has not been validated
  end to end.

## Safety

This project does not solve captchas, bypass verification or authentication
challenges, extract credentials, rotate proxies, or disguise multi-account
operation. Stop the runner and review the account when Weibo asks for human
verification. Use the publisher only with an account you are authorized to
operate and in accordance with Weibo's rules.

## Privacy

The Playwright profile contains live Weibo session data. Never commit or share
`runtime/profile`, `runtime/state.db`, `runtime/logs`, `runtime/artifacts`, or a
local `config.toml`. These paths are ignored by Git, but ignored files can
still be exposed if they are copied, attached, or archived manually.

If session data is exposed, stop the runner, attempt to revoke the session
through Weibo, remove `runtime/profile`, and log in again.

## License

This project is available under the MIT License. See `LICENSE`.
