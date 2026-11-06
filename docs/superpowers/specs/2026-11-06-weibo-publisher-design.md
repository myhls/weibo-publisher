# Weibo Publisher Design

Date: 2026-11-06

## 1. Purpose

Build a local, single-account Weibo publishing robot for Windows. The user
creates a post task through a CLI or Codex, confirms it once, and the robot
publishes it unattended at the requested time.

The first release prioritizes a reliable one-post-at-a-time workflow over
multi-account management, AI content generation, or a web dashboard.

## 2. Goals

- Publish text and image posts to one Weibo account.
- Support public visibility by default and retain a visibility field for future
  expansion.
- Accept ISO 8601 timestamps with an explicit timezone.
- Default task scheduling to Asia/Shanghai.
- Reuse a dedicated Playwright browser profile created by QR-code login.
- Require explicit task confirmation before scheduling.
- Publish unattended at the confirmed time.
- Record a verifiable Weibo result URL when possible.
- Prevent duplicate publication after crashes or ambiguous responses.
- Save logs, screenshots, page snapshots, and failure artifacts.
- Keep the project suitable for later publication on GitHub under MIT.

## 3. Non-Goals

- Multiple Weibo accounts.
- Automatic content generation.
- A local web dashboard.
- Mobile/emulator automation.
- Captcha solving or bypassing platform security controls.
- Proxy rotation or anti-detection systems.
- Official Weibo Open Platform integration.
- Guaranteed support for 100 posts per day on one account.
- A guarantee that Weibo will not rate-limit or restrict an account.

## 4. Constraints

- The publisher runs only while the computer is powered on and the runner
  process is active.
- Weibo DOM and network behavior may change without notice.
- Login sessions eventually expire and require another QR-code login.
- Weibo may show captcha, verification, rate-limit, or content-review screens.
- The robot must stop rather than bypass any verification or security prompt.
- Browser profile data is sensitive and must never be committed.

## 5. Technology

- Python 3.11
- Playwright async API
- APScheduler 3.x
- SQLite through the Python standard library
- `zoneinfo` for timezone handling
- `argparse` for the command-line interface
- `pytest`, `pytest-asyncio`, and `ruff` for development

Playwright will use a dedicated persistent browser profile. The default
browser channel is Microsoft Edge on Windows, with Chromium available as a
configuration fallback.

## 6. Project Layout

```text
weibo-publisher/
├─ docs/
│  └─ superpowers/specs/
├─ src/weibo_publisher/
│  ├─ cli.py
│  ├─ models.py
│  ├─ repository.py
│  ├─ scheduler.py
│  ├─ browser.py
│  ├─ weibo.py
│  ├─ config.py
│  └─ logging_setup.py
├─ tests/
├─ runtime/
│  ├─ profile/
│  ├─ artifacts/
│  ├─ logs/
│  └─ state.db
├─ config.example.toml
├─ pyproject.toml
├─ README.md
├─ SECURITY.md
├─ LICENSE
└─ .gitignore
```

`runtime/` is excluded from Git except for a committed `.gitkeep` if needed.

## 7. CLI

The first release exposes these commands:

```text
weibo-publisher login
weibo-publisher doctor
weibo-publisher add ...
weibo-publisher show JOB_ID
weibo-publisher confirm JOB_ID
weibo-publisher preview JOB_ID
weibo-publisher list [--status STATUS]
weibo-publisher cancel JOB_ID
weibo-publisher run
weibo-publisher status JOB_ID
```

### login

Opens a headed Playwright browser using the dedicated profile and navigates to
the Weibo login page. The user scans a QR code or completes login manually.
The command never asks for or stores a password.

### doctor

Checks:

- Python and required packages
- Browser executable/channel
- Runtime directory permissions
- Database access
- Whether the Weibo session appears valid

### add

Creates a `draft` task. Required options:

- `--text`
- `--at` as ISO 8601
- one or more `--image`

Optional:

- `--visibility public|followers`
- `--timezone Asia/Shanghai`
- `--file` for a JSON task description

The command prints the generated job ID and a preview. It does not schedule the
job until `confirm` is run.

### confirm

Validates the task and transitions it from `draft` to `approved`. It stores a
fingerprint of:

- normalized text
- SHA-256 hashes of image files
- publish timestamp
- visibility

Changing any of these after confirmation invalidates the approved task.

### preview

Performs a safe Weibo UI rehearsal:

- verifies login
- opens the composer
- enters the text
- attaches images
- verifies the selected visibility
- captures a screenshot
- does not click publish

### run

Starts the long-running scheduler. It must be the only runner instance for the
profile. A file lock prevents concurrent runners.

### status

Shows job state, attempts, artifacts, last error, and Weibo URL.

## 8. Job Model

Each job stores:

- `id`: UUID
- `text`: post body
- `images`: JSON array of local paths
- `publish_at_utc`: canonical UTC timestamp
- `timezone`: IANA timezone name
- `visibility`: public or followers
- `status`: state machine value
- `fingerprint`: confirmation fingerprint
- `created_at`
- `confirmed_at`
- `last_attempt_at`
- `published_at`
- `weibo_mid`
- `weibo_url`
- `last_error`
- `artifact_dir`

Each publish attempt stores:

- `job_id`
- `attempt_id`
- `started_at`
- `finished_at`
- `result`: success, failed, unknown, interrupted
- `error`
- artifact paths

## 9. State Machine

```text
draft -> approved -> preparing -> ready -> publishing -> published
```

Exceptional transitions:

```text
draft -> cancelled
approved -> cancelled
approved -> missed
preparing -> needs_attention
preparing -> failed
ready -> needs_attention
publishing -> failed
publishing -> unknown
unknown -> needs_attention
```

Rules:

- Only `approved` tasks are scheduled.
- A task modified after confirmation returns to `draft`.
- A late task does not publish automatically. If the runner starts after the
  target time plus the 30-second cutoff, it becomes `missed`.
- Once `publishing` starts, the task cannot return to `approved`
  automatically.
- `unknown` requires human verification and is never retried automatically.

## 10. Scheduling

The scheduler rescans SQLite every five seconds so tasks can be added,
confirmed, or cancelled while the runner is active.

For each approved task:

1. Schedule a preparation job at `publish_at - 3 minutes`.
2. At preparation time, transition to `preparing` using a conditional database
   update.
3. Validate images, fingerprint, daily limits, and login state.
4. Open the composer and fill the post.
5. Capture a pre-publish screenshot and transition to `ready`.
6. Wait until `publish_at`.
7. Transition to `publishing`.
8. Click publish and await the network response.
9. Record success, failure, or unknown outcome.

If the target time is already in the past when scheduling:

- More than 30 seconds late: mark `missed`.
- Within 30 seconds and no publication attempt exists: the scheduler may
  continue only if preparation is already complete.

## 11. Rate and Density Controls

Configuration includes:

- `daily_limit`, default 20
- `minimum_gap_seconds`, default 60
- `allow_over_limit`, default false

`add` and `confirm` warn when a task would exceed a soft limit. Publishing does
not proceed when a hard limit is enabled and exceeded.

These controls reduce accidental bursts but do not make high-volume publishing
safe. The README must state that Weibo may restrict an account regardless of
local limits.

## 12. Browser and Login Session

- Use `launch_persistent_context` with `runtime/profile`.
- Default to headed mode for login, preview, and initial production use.
- Headless mode is configurable but not the default until validated with the
  account.
- The profile contains cookies, local storage, and other personal data.
- The profile is never copied into the repository or included in diagnostics.
- A session check loads Weibo and verifies that the user is not redirected to
  a login or verification page.

## 13. Weibo Adapter

The adapter owns all Weibo-specific selectors and network patterns.

Required operations:

- `open_composer()`
- `fill_text(text)`
- `attach_images(paths)`
- `set_visibility(visibility)`
- `submit()`
- `detect_result()`

The adapter must prefer semantic selectors and stable attributes. Broad
class-name selectors are allowed only as fallbacks and must be covered by
manual smoke tests.

Success detection:

- Observe the POST request to the Weibo publish endpoint.
- Parse the JSON response.
- Treat the documented success code as success only when it contains a post ID
  or result URL.
- If the click happened but no unambiguous success or failure can be
  established, return `unknown`.

The adapter must not solve captcha, bypass verification, or interact with
authentication dialogs.

## 14. Concurrency and Idempotency

- A runner lock file prevents multiple runner processes.
- Job transitions use conditional SQL updates.
- A job can transition from `approved` to `preparing` only once.
- A job can transition from `ready` to `publishing` only once.
- The scheduler reload does not create duplicate jobs.
- A crash during `publishing` becomes `unknown` on restart.
- No automatic retry is allowed after an ambiguous submit.

## 15. Artifacts and Logging

For each attempt, save under:

```text
runtime/artifacts/<job_id>/<attempt_id>/
```

Artifacts include, as applicable:

- preparation screenshot
- pre-submit screenshot
- result screenshot
- page HTML snapshot
- error text
- Playwright trace on failure

Logs are written to `runtime/logs/` and include job ID, state transition,
timestamp, and error category. Logs must not include cookies or full page
storage state.

## 16. Configuration

`config.example.toml` contains:

- runtime directory
- database path
- browser channel
- headless flag
- default timezone
- preparation lead time
- late cutoff
- daily limit
- minimum gap
- artifact retention count

The real configuration lives outside Git or is ignored.

## 17. Security and Privacy

- Never request or store the Weibo password.
- Never commit `runtime/profile`, `state.db`, artifacts, or logs.
- Do not expose login state through debug output.
- Do not use the user's normal Edge profile.
- Do not automatically retry after uncertainty.
- Do not add captcha or verification bypass features.
- Document that users are responsible for complying with Weibo's rules.

## 18. Testing

### Unit Tests

- Task validation
- Content fingerprint changes
- Timezone conversion
- State transitions
- Daily limit and minimum gap
- Conditional transition idempotency
- Late-task handling
- Unknown result handling

### Local Integration Tests

Use a local HTML form with:

- text input
- file input
- visibility selector
- publish button
- mock success response

Verify the full prepare-wait-submit-record workflow without contacting Weibo.

### Manual Weibo Smoke Tests

1. Run `login`.
2. Run `doctor`.
3. Create a draft task.
4. Run `preview` and inspect the screenshot.
5. Confirm a test task.
6. Run `run` and verify one real post.
7. Verify the saved URL and absence of duplicate posts.

No real post is made without explicit user approval.

## 19. Open-Source Readiness

- MIT License
- README with setup, usage, limitations, and safety warnings
- SECURITY.md
- `.gitignore` for profile, database, artifacts, logs, and local config
- Example task data with no personal information
- CI for unit tests and linting when GitHub Actions is added

If any code is copied or adapted from another MIT project, its copyright and
license notice must be retained.

## 20. Acceptance Criteria

The first release is complete when:

- One account can log in through a dedicated persistent profile.
- A draft task can be created, previewed, and confirmed.
- A text-and-image post publishes at the requested Beijing time.
- The runner records a result URL or a precise failure reason.
- A restart cannot publish the same task twice.
- An ambiguous submit becomes `unknown` and is not retried.
- Unit and local integration tests pass.
- A real post is performed only after explicit user approval.
- Sensitive runtime data is excluded from Git.

## 21. Future Work

- Batch import and richer schedule views
- Multiple accounts with independent profiles
- Optional native Weibo scheduled-post adapter
- Windows startup task installer
- Notification hooks
- Optional local web dashboard
