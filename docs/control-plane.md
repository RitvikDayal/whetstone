# The control plane

```
whetstone ui
```

Opens a local page showing the same queue `whetstone findings` prints. It binds
`127.0.0.1`, mints a fresh token every time it starts, and dies with the
terminal you ran it in.

Install it with the `ui` extra — it is not part of the base install, for the
same reason the browser lens is not:

```
pip install 'whetstone-cli[ui]'
```

| Flag | What it does |
|---|---|
| `--no-open` | Do not launch a browser. Prints the address without the token. |
| `--print-url` | Print the full address **including the session token**. |
| `--port N` | Listen on a specific port. The default, `0`, lets the OS pick. |

---

## Why a local server needs a password

The short version: **localhost is not a security boundary.** Any web page open
in your browser can make requests to a server on your own machine, and there
are two separate ways that goes wrong.

**Cross-site request forgery.** A page on any site can issue a request to
`http://127.0.0.1:7727/`. It cannot read the answer, but for anything that
*changes* something, not being able to read the answer is not a defence.

**DNS rebinding.** An attacker's page loads from `evil.example`, whose DNS then
re-points at `127.0.0.1`. To the browser it is now the *same origin* as your
control plane, so the same-origin policy stops protecting you and the page can
read responses. This is not hypothetical: it is the class that hit Vite's dev
server, and it produced a critical advisory against the MCP TypeScript SDK in
December 2025 — an HTTP-only local server with no `Host` check, which is exactly
the shape a naive version of this would have.

### There are two controls, and they do not overlap

| Attack | The only thing that stops it |
|---|---|
| Any web page issuing writes to the API | **The session token** |
| An attacker's domain re-resolving to `127.0.0.1` | **The `Host` check** |

Two other properties look like controls and are not, and it is worth being
explicit because the temptation is to count them:

- **Binding `127.0.0.1`** stops nothing browser-mediated. Your browser is
  already on the loopback interface.
- **Sending no CORS headers** stops an attacker *reading* a response. The
  request still arrives and still takes effect. A cross-origin `GET`, or a
  `POST` with an ordinary content type, is a "simple request": no preflight, the
  handler runs, only the response is withheld.

So the token is a single point of failure for every write, and the `Host` check
is a single point of failure for rebinding. **Neither substitutes for the
other, and there is no third thing behind them.**

### How the token travels

`whetstone ui` opens `http://127.0.0.1:<port>/#t=<token>`. The token is in the
URL **fragment**, which browsers never send to a server and never put in a
`Referer` header — so it does not appear in an access log, a proxy log, or an
outbound request. The page reads it, strips it from the address bar, and keeps
it in `sessionStorage`: scoped to that tab, surviving a reload, gone when the
tab closes.

Every API request carries it in an `X-Whetstone-Token` header. A custom header
is the point — a cross-origin form cannot set one, and a cross-origin `fetch`
that tries triggers a preflight this server answers with nothing.

**The token is not printed** unless you ask for it with `--print-url`. Terminal
scrollback gets screen-shared, piped through `tee`, captured by
`Start-Transcript`, and pasted into chat windows. Anyone who reads that line can
act on your project.

### What this means for you

- **Do not paste the URL anywhere.** Re-run `whetstone ui` instead; the token
  changes every time.
- **A second tab, or a bookmark, will not work.** That is deliberate — the
  token is per tab and per session. The page says so rather than going blank.
- **Do not put this behind a reverse proxy** to reach it from another machine.
  The `Host` check will refuse it, and that check is one of only two things
  standing between a web page and your repository.

---

## What the screen shows, and what it deliberately does not

The findings list is the **same list, in the same order**, as
`whetstone findings`. Both render from one projection
(`whetstone/readmodel.py`), and `tests/unit/test_surface_parity.py` plus
`tests/unit/test_control_plane_render.py` measure that the terminal, the JSON
and the rendered DOM agree — the second of those drives a real browser, because
every time this project has lost a verdict it lost it at the render layer.

Three things the screen is careful about:

- **`killed` is a word, not a letter.** A grade D once rendered indistinguishably
  from a grade A in the CLI. The verdict column spells it out.
- **"not graded" is not "killed".** `hygiene` does not grade at all, and showing
  an ungraded CVE as refuted would report a measured fact as dismissed by a
  stage that never looked at it.
- **A run that did not check everything says so**, above the list, before you
  read a single row.

The list is **read-only in this release.** Deciding on findings, starting runs,
and the trust and cost screens land next; until then `whetstone decide` and
`whetstone run` are the way to act.

### `whetstone.yaml` is never edited from the browser

Config editing is the highest-risk endpoint this surface could have — a write
into your project root, driven from a page. `whetstone init` already writes that
file and verifies every answer by executing it, and duplicating it behind HTTP
doubles the surface to buy convenience.

For the same reason, when the setup view arrives it will run only the checks
that **do not execute your project's commands**. `whetstone doctor` runs
`install`, `test`, `lint` and `build` for real, in your project root, through a
shell — `install` and `build` write into the project, and on Windows a
repo-local `git.bat` beats `git.EXE` on `PATH` (measured, and recorded in
`doctor.py`). That is fine for a command you typed. It is not fine for a button.

---

## When it will not start

**"the control plane needs FastAPI and uvicorn"** — the `ui` extra is not
installed. `pip install 'whetstone-cli[ui]'`.

**"the control plane's built assets are not present"** — the Python package is
fine; the JavaScript bundle is missing. From a source checkout:

```
npm --prefix src/whetstone/ui ci
npm --prefix src/whetstone/ui run build
```

From an installed wheel, that wheel was built without its front-end — reinstall
from a release wheel. These are two different failures with two different fixes,
and the message says which one you have.

**A blank page** should not happen, and if it does it is a bug worth filing: the
Content-Security-Policy is strict (`default-src 'none'`, no `'unsafe-inline'`)
precisely because this page renders model-authored text read out of a repository
you did not necessarily write, and a CSP that tight is a CSP that can break the
page it protects. `tests/unit/test_control_plane_render.py` loads the real
bundle in Chromium and fails on any console error, so a regression there is
caught rather than shipped.
