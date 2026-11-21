# Deployment notes

This document answers one question: **where can the Traffora-AI orchestrator
actually run?** The short version is that Render (or Railway, or Fly.io) can
host it, and Vercel cannot. The reasons are architectural rather than
configuration details, so they are worth stating plainly.

## Why Vercel cannot host this

Vercel is a platform for static assets and short-lived serverless functions. A
request arrives, a function runs for at most a few minutes, and the process is
frozen or discarded. The orchestrator does not fit that model for three
independent reasons:

1. **It owns a long-lived child process.** `SimulationCoordinator.start()`
   launches `sumo` as a subprocess and speaks to it over a TCP socket using
   TraCI. Serverless functions are not allowed to keep background processes
   alive between invocations, and the filesystem they see is read-only apart
   from `/tmp`.
2. **It holds state in memory across requests.** The shared `SharedState`
   object, the event queue, the decision history and the live SUMO connection
   all live in the Python process. A serverless platform may route consecutive
   requests to different instances, so `/simulation/step` would not see the
   simulation that `/simulation/start` created.
3. **It is far above the size limit.** The `eclipse-sumo` wheel alone is
   roughly 150 MB, before scikit-learn, pandas and the three agents' pickled
   models. Vercel's serverless bundles are capped well below that.

You *could* deploy the static dashboard to Vercel and point it at an API hosted
elsewhere, but that is a two-platform split, not a Vercel deployment of this
service. If you want a single URL for judging, use Render.

## Why Render works

Render runs a real Linux container from a `Dockerfile`, which is exactly the
execution model the orchestrator needs. Two things had to be true for this to
work, and both now are:

- **SUMO must be installable without an MSI.** It is. `pip install eclipse-sumo`
  provides a `manylinux_2_28_x86_64` wheel that ships the `sumo` and
  `netconvert` binaries inside the package and exposes their directory as
  `sumo.SUMO_HOME`. The repository's SUMO lookup already falls back to that
  attribute when `SUMO_HOME` is unset, so no environment variable is needed.
- **The server must honour the injected port.** Render assigns a port through
  `$PORT` and expects the process to bind `0.0.0.0`. `backend/orchestrator/api.py`
  now does both, while keeping the loopback default for local runs.

The repository ships three files for this: `Dockerfile`, `.dockerignore` and
`render.yaml`. The Dockerfile also fails the build early if the SUMO binary is
missing, so a broken image never reaches a running service.

### Deploying on Render

1. Push the `integration` branch to GitHub (see the git note in
   `backend/orchestrator/README.md`).
2. In the Render dashboard choose **New → Blueprint**, select the repository,
   and pick the `integration` branch. Render reads `render.yaml` and builds the
   `Dockerfile`.
3. Wait for the first build (the SUMO wheel makes it slow, several minutes).
   The health check at `/health` should turn green.
4. Open the service URL. `GET /health` returns `{"status": "ok"}` and
   `GET /status` returns the orchestrator's status object.

If you prefer not to use a Blueprint, create a **Web Service** manually, set
the runtime to **Docker**, and leave the start command empty — the Dockerfile's
`CMD` is correct on its own.

### Deploying on Railway or Fly.io

Both accept a `Dockerfile` unchanged. Railway detects it automatically; Fly
needs a one-line `fly.toml` with `internal_port = 8000` and
`[http_service] force_https = true`. Neither requires code changes.

### Caveats worth knowing before a demo

- The Render **free** plan sleeps after about fifteen minutes of inactivity and
  gives you 512 MB of RAM. Waking takes tens of seconds, and the in-memory
  simulation state is lost. For a judged demo, either use a paid instance or
  keep the demo local and use Render only to show that it deploys.
- The simulation runs on a fixed seed (`42` by default), so results are
  reproducible, but wall-clock timing differs from a laptop.
- Only one simulation can run per process. That is a deliberate design
  constraint — one shared SUMO session — not a deployment limitation.

## Local deployment check

Before deploying, confirm the container behaviour without Docker by running the
entry point with `PORT` set, which is what Render does:

```powershell
$env:PORT = "8123"
python -m backend.orchestrator.api
```

The log line should read `Uvicorn running on http://0.0.0.0:8123`, and
`http://127.0.0.1:8123/health` should return `{"status":"ok"}`. Unset `PORT`
afterwards to return to the loopback default.
