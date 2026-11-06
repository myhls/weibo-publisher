# Security Policy

## Sensitive data

The Playwright profile contains live Weibo session data, including cookies and
local storage. Never share or commit `runtime/profile`, `runtime/state.db`,
`runtime/logs`, or `runtime/artifacts`. The state database and artifacts can
contain post content, local paths, screenshots, and failure details. A local
`config.toml` may also contain machine-specific information and must remain
private.

If you expose sensitive session or state data, stop the runner, attempt to
revoke the Weibo session, remove the exposed runtime data, and log in again.

## Reporting

Open a private security advisory instead of publishing credentials or session
data in a public issue. On GitHub, use the repository's Security tab and select
the private vulnerability reporting option. Include a concise description,
affected version or commit, reproduction steps, and any relevant sanitized
logs.

Do not include account credentials, cookies, browser-profile files, access
tokens, or unredacted personal data in a report. If a private reporting channel
is unavailable, ask the maintainer for a private contact method without posting
the sensitive details publicly.

## Supported behavior

This project does not support captcha bypass, credential extraction, proxy
rotation, or disguised multi-account operation. Requests to add or use those
capabilities are outside the project's security and support scope.

Security reports are still welcome for issues such as accidental credential
exposure, unintended publication, unsafe logging, state corruption, and
verification-flow handling that could cause an unintended post.
