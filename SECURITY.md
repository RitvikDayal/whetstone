# Security

## Reporting

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/RitvikDayal/whetstone/security/advisories/new),
not as a public issue.

This is a single-maintainer project. Expect an acknowledgement within a week,
and an honest answer about timing rather than an SLA it cannot keep.

## What Whetstone can already do, by design

Read this before deciding whether something is a vulnerability. Several of these
look alarming and are the documented behaviour.

**It executes your project's own commands.** `whetstone doctor` runs the
`install`, `test`, `lint` and `build` commands from your `whetstone.yaml`
through a shell, in your project root. That is the point — the difference
between "configured correctly" and "ran correctly" is the reason the command
exists.

**A repository can hijack those commands on Windows.** `shell=True` with
`cwd=project_root` makes cmd.exe resolve the current directory before `PATH`.
Measured: with a `git.bat` in the project root, `git --version` executed the
repository's file rather than `git.EXE`. Windows ships a mitigation
(`NoDefaultCurrentDirectoryInExePath`) and it is unset by default.

The boundary is **who controls the working directory**, not who wrote the
config. Running `whetstone init` or `whetstone doctor` in a repository you do
not trust runs that repository's code. This is recorded in `doctor.py` and is
not a bug report.

**Model-authored text reaches your terminal and your browser.** Findings are
written by a model reading a repository you may not have written. Control
characters are escaped before they reach the console, and the control plane
renders everything as text under a strict CSP. A way around either of those
*is* a vulnerability.

## The control plane

`whetstone ui` binds `127.0.0.1` and requires a per-session token on every API
call. **Localhost is not a security boundary** — any page open in your browser
can reach a local server. There are exactly two controls and they do not
overlap:

| Attack | The only thing that stops it |
|---|---|
| Any web page issuing writes to the API (CSRF) | The session token |
| An attacker's domain re-resolving to `127.0.0.1` (DNS rebinding) | The `Host` check |

Two things that look like controls and are not: binding loopback stops nothing
browser-mediated, and emitting no CORS headers stops an attacker *reading* a
response, never the request arriving and taking effect.

It is **plain HTTP**, deliberately. Reading loopback traffic needs code
execution on the machine, at which point the token is readable from process
memory anyway; and the alternatives — a certificate interstitial on every start,
or a permanent machine-wide root CA — are larger risks than the one they buy
off. Browsers treat `http://127.0.0.1` as a secure context for the same reason.

The full reasoning is in `src/whetstone/server/security.py` and
[docs/control-plane.md](docs/control-plane.md).

**Do not expose it beyond loopback.** There is no configuration that makes it
listen elsewhere, and the `Host` check will refuse a reverse proxy.

## What is in scope

- Anything that lets a repository under analysis escape the boundaries
  `whetstone.yaml` declares — particularly a write outside the worktree, or past
  `never_touch`.
- Anything that reaches the control plane's API without the session token, or
  from a foreign `Host`.
- Script execution in the control plane page, or a way past the CSP.
- Anything that causes a credential to be written to disk, logged, or printed.
- A published release whose artifact does not match its tag.

## What is out of scope

- The behaviours in "What Whetstone can already do" above.
- Anything requiring an already-compromised maintainer account. The release
  gate stops an *unattended* publish — a script, a mistyped command, a
  workflow. It does not stop a compromised account, and no configuration of a
  single-maintainer repository can. The honest fix is a second maintainer.
- Findings against a dependency, unless Whetstone's use of it is what makes it
  exploitable. Report those upstream.

## Supported versions

Pre-release. Only the latest tag is supported, and there are no backports.
