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

For Chinese text or multi-line copy, keep the post body in a UTF-8 text file and
pass the file path instead:

```powershell
python -m weibo_publisher add --text-file "D:\path\post.txt" --image "D:\path\image.jpg" --at "2026-11-07T17:30:00+08:00"
```

New jobs must be scheduled at least five minutes in advance. This leaves enough
time for review and the three-minute preparation window.

Repeat `--image` to attach more than one image. The publish time must be an ISO
8601 timestamp with a timezone. `add` creates a draft and does not publish it.

Use `--visibility public|private` to choose the audience. The default is
`public`. `private` means Weibo `仅自己可见`:

```powershell
python -m weibo_publisher add --text "仅自己可见" --image "D:\path\image.jpg" --at "2026-11-07T17:30:00+08:00" --visibility private
```

Followers, friends-circle, and group visibility are not supported.

### Scheduled prepare-only rehearsal

Add `--rehearsal` to run the real scheduler preparation path without clicking
the Weibo publish button:

```powershell
python -m weibo_publisher add --text "调度演练" --image "D:\path\image.jpg" --at "2026-11-07T17:30:00+08:00" --visibility private --rehearsal
python -m weibo_publisher confirm JOB_ID
python -m weibo_publisher run
```

The job prepares at the normal three-minute lead time, verifies content,
images, and visibility, then ends as `rehearsed`. Rehearsal jobs do not count
toward the daily publication limit or minimum publication gap.

## Show and confirm

```powershell
python -m weibo_publisher show JOB_ID
python -m weibo_publisher confirm JOB_ID
```

Review the text, images, time, and visibility before confirming. Real
publishing requires explicit task confirmation; only approved jobs are
scheduled. Confirmation also requires enough time for the three-minute
preparation window.

## Batch workbook

Import the workbook into draft jobs first:

```powershell
python -m weibo_publisher batch-import `
  --file "D:\path\weibo-batch.xlsx" `
  --date 2026-11-11
```

The command reads the `发布清单` sheet, validates every row, and creates all
parseable jobs as drafts. It prints the total count, complete schedule, and
every blocking issue. No job is approved by this command.

Review the current workbook and stored draft together:

```powershell
python -m weibo_publisher batch-report BATCH_ID
```

The report re-reads the workbook each time. It checks text, all 18 image
columns, visibility, Beijing publish time, image existence and size, detected
file format, duplicate text or image references, internal body time, and the
240-second local spacing rule.

Approve the entire validated batch atomically:

```powershell
python -m weibo_publisher batch-confirm BATCH_ID --yes
```

The command revalidates the workbook and refuses approval if the source changed
after import or any blocking issue remains. Approval succeeds only when every
job in the batch can transition from draft to approved. The job-level
`confirm` command cannot approve batch jobs individually.

If a blocking issue is fixed in the workbook, replace the existing draft batch:

```powershell
python -m weibo_publisher batch-import `
  --file "D:\path\weibo-batch.xlsx" `
  --date 2026-11-11 `
  --replace
```

List imported batches with:

```powershell
python -m weibo_publisher batch-list
```

Before any job has entered preparation or been published, cancel the remaining
jobs with:

```powershell
python -m weibo_publisher batch-cancel BATCH_ID --yes
```

All publication still runs through the same single runner and dedicated
profile. Batch jobs use a shared serial execution lock. While one job holds the
browser, no other job can open the same profile.

If login expires, a captcha appears, or Weibo shows a verification or
rate-limit response, the runner halts and the batch becomes `paused`. Confirm
the account state manually, then use `batch-resume BATCH_ID --yes` only after
the current source still validates. An interrupted preparation or ambiguous
submission is not retried automatically.

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

When preparation fails, the artifact directory also contains:

- `failure.png`
- `diagnostics.json`
- `composer.html`
- `visibility-popovers.json`

These files remain under the Git-ignored `runtime/` directory.

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
- Live publishing was smoke-tested with one private text-and-image post, but
  broader content, account, and visibility combinations have not been
  validated end to end.

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
