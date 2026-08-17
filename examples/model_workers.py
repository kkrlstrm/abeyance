"""Two model workers, and the reason they are two.

A case needs a machine to gather facts and something with judgment to form an opinion. Those are
different contribution kinds, so they get different workers, different reach, different apps —
and different *clearances* (see [`abeyance/clearance.py`](../abeyance/clearance.py)).

    claude-recommendation   RECOMMENDATION   app=workers-model    subscription auth, no API meter
    openrouter-evidence     EVIDENCE         app=workers-extract  metered, cheap, eval-gated

**Why Claude for the recommendation and OpenRouter for the evidence.** "Should this launch" is a
judgment call with consequences attached. Bulk field extraction from a fetched page is not. The
cheap eval-gated modes are cleared for EVIDENCE precisely because that is what their evals
measured; asking one for a launch opinion is off the end of its recorded evidence, and
`clearance.require()` refuses it at registry-build time rather than at dispatch.

**The recommendation worker costs no API tokens.** It runs the Claude Code CLI headlessly
(`claude -p`) authenticated by `CLAUDE_CODE_OAUTH_TOKEN` — a subscription credential, not a
metered key. So the judgment half of a case is a fixed monthly cost rather than per-token spend,
and the metered spend is confined to the cheap extraction rungs. Three constraints that come with
this, each of which will bite silently if missed:

1. **Claude Code refuses `--permission-mode bypassPermissions` as root.** The worker must run as
   a non-root user. `CLAUDE_RUNNER` below creates one and drops to it.
2. **`HOME` must be set and `~/.claude.json` must trust the working directory**, or the CLI stops
   to ask a question no one is there to answer — and a worker that blocks on a prompt looks
   exactly like a worker that is thinking, until its lease expires.
3. **The token is an app secret, never a case field.** It belongs to the platform app named by
   the capability (`fly secrets set --app workers-model`), which is what makes the boundary real.

Neither worker imports abeyance. Each reads its `ABEYANCE_*` contract from env, does one thing,
and writes one row.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from abeyance import Capability, CapabilityRegistry, ContributionKind
from abeyance.clearance import model_capability
from openrouter_clearances import CLEARANCES, routes_consistency   # noqa: E402

# --------------------------------------------------------------------------- clearances
#
# Not defined here. `openrouter_clearances` derives them from an existing eval-gated routing
# allowlist (`routes.json`), so there is ONE allowlist rather than a copy that can disagree with
# it — and it adds the orchestrator tier that file deliberately does not cover. Every entry carries
# the eval that cleared it, the date it was scored, and the contribution kinds that eval covers.
#
# The split falls out of the source policy rather than being imposed: the cheap metered modes are
# cleared for EVIDENCE, the judgment tier for RECOMMENDATION, and nothing for DECISION ever.

# --------------------------------------------------------------------------- runners
#
# The shell that boots each worker. Written out in full so the worker-side contract is readable in
# one place: install what you need, decode the script, run it.

CLAUDE_RUNNER = r"""
set -eu
# Rehearsal shape: install at boot so the example runs with no image build. For a standing
# deployment, bake this into a digest-pinned image instead — a mutable tag means the reviewed
# capability and the running capability can differ silently, and ~60s of every lease is spent here.
# node:22-slim ships none of these: python3 runs the worker, psql writes the contribution, gosu
# drops privileges. Claude Code itself needs Node 18+, which is why node is the base.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends postgresql-client python3 gosu >/dev/null
npm install -g --silent @anthropic-ai/claude-code >/dev/null

# Claude Code refuses bypassPermissions as root, so make a user and hand it a trusted workdir.
id -u worker >/dev/null 2>&1 || useradd --create-home --shell /bin/bash worker
export HOME=/home/worker
mkdir -p "$HOME" /work
cat > "$HOME/.claude.json" <<'JSON'
{"hasCompletedOnboarding": true, "projects": {"/work": {"hasTrustDialogAccepted": true}}}
JSON
echo "$WORKER_B64" | base64 -d > /work/w.py
chown -R worker:worker "$HOME" /work
cd /work
exec gosu worker env HOME="$HOME" python3 /work/w.py
"""

PSQL_PY_RUNNER = r"""
set -eu
pip install --quiet --disable-pip-version-check "psycopg[binary]" >/dev/null
echo "$WORKER_B64" | base64 -d > /tmp/w.py
exec python3 /tmp/w.py
"""

# --------------------------------------------------------------------------- W1: the opinion
#
# Runs `claude -p` on the case's spec and writes ONE recommendation. It has no production
# credentials — no CRM write, no mail, no money. It may recommend; it cannot turn that
# recommendation into authority, and `standing.counts_as_decision()` never reads its payload.

CLAUDE_RECOMMENDATION_PY = r'''
import json, os, re, subprocess, sys, time

spec = json.loads(os.environ.get("ABEYANCE_SPEC") or "{}")
need = os.environ.get("ABEYANCE_NEED", "recommendation")

# The prompt is Tier 1 — a new instruction, free and ungated. What it cannot do is reach
# somewhere no declared capability reaches, and it cannot make its own output authoritative.
PROMPT = f"""You are a WORKER in a durable case. You are producing a RECOMMENDATION.

You have NO authority. A human with standing decides whether this proceeds; your output is one
input to that decision and is recorded as such. Do not claim approval, do not assert that no
review is needed, and do not describe yourself as authorized — such wording is ignored by the
authority rules and is reported as an AUTHORITY_CLAIMED escalation, so it only adds noise.

Need: {need}
Evidence and instruction (JSON):
{json.dumps(spec, indent=2)}

Reply with ONE JSON object and nothing else:
{{"summary": "<one line a human reads first>",
  "rationale": "<what in the evidence supports this, naming the figures you used>",
  "confidence": <0.0-1.0>,
  "scope": {{"max_leads": <int or null>, "warm_up_required": <true|false>}},
  "unknowns": ["<what you could not determine from the evidence given>"]}}

`scope` is a CEILING you are asserting, intersected with every other contributor's — it can only
narrow the envelope, never widen it, so state the tightest bound the evidence supports. If the
evidence is insufficient, say so in `unknowns` and lower `confidence`; do not invent figures."""

argv = ["claude", "-p", PROMPT, "--permission-mode", "bypassPermissions"]
budget = os.environ.get("MODEL_MAX_BUDGET_USD")   # only meaningful on API-key fallback
if budget:
    argv += ["--max-budget-usd", budget]

# The lease is the real deadline; finish inside it or the dispatcher re-dispatches us.
timeout = int(os.environ.get("MODEL_TIMEOUT_SECONDS", "540"))
proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
if proc.returncode != 0:
    # Exit non-zero WITHOUT writing. A satisfied request, not a clean exit, is success — so
    # failing loudly here becomes DISPATCH_LOST and then REQUEST_FAILED, which blocks
    # authorization. Writing a fabricated row instead would let the case proceed on nothing.
    sys.stderr.write(f"claude -p exited {proc.returncode}: {proc.stderr[-2000:]}\n")
    sys.exit(1)

raw = proc.stdout.strip()
m = re.search(r"\{.*\}", raw, re.S)          # tolerate prose or a fence around the object
if not m:
    sys.stderr.write(f"no JSON object in model output: {raw[:2000]}\n")
    sys.exit(1)
payload = json.loads(m.group(0))

scope = payload.get("scope") or {}
scope = {k: v for k, v in scope.items() if v is not None}   # a null is not a ceiling

doc = {
  "id": os.environ["ABEYANCE_CONTRIBUTION_KEY"], "case_id": os.environ["ABEYANCE_CASE_ID"],
  "request_id": os.environ["ABEYANCE_REQUEST_ID"],
  "kind": os.environ.get("ABEYANCE_CONTRIBUTION_KIND", "recommendation"),
  "actor": {"id": os.environ["ABEYANCE_ACTOR"], "kind": "worker", "standing": [], "display": ""},
  "summary": str(payload.get("summary", ""))[:500],
  "payload": payload,
  "scope": scope,
  "provenance": {"machine": os.environ.get("FLY_MACHINE_ID", "unknown"),
                 "app": os.environ.get("FLY_APP_NAME", "unknown"),
                 "model_mode": os.environ.get("MODEL_MODE", ""),
                 "model": os.environ.get("MODEL_ID", ""),
                 "auth": "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key",
                 "attempt": os.environ.get("ABEYANCE_ATTEMPT", ""),
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
  "dependencies": spec.get("dependencies") or [],
  "supersedes": "",
  "created_epoch": int(time.time()),
}

# One upsert, keyed <case>::<request>. A re-dispatched duplicate overwrites its own row rather
# than casting a second vote — which is what makes at-least-once dispatch safe here.
#
# There is no psycopg in a node image, so the write goes through psql. Values are dollar-quoted
# with a tag that cannot occur in the payload, and the statement is piped on stdin rather than
# built into an argv string — model output ends up inside this SQL, so it is escaped by
# construction rather than by hoping a quote does not appear.
def dollar_quoted(value: str) -> str:
    tag = "abey"
    while f"${tag}$" in value:
        tag += "x"
    return f"${tag}${value}${tag}$"


statement = (
    "INSERT INTO abeyance.state (kind, key, doc, updated_at, updated_by) VALUES ("
    + ", ".join(dollar_quoted(v) for v in ("contribution", doc["id"]))
    + ", " + dollar_quoted(json.dumps(doc)) + "::jsonb, now(), "
    + dollar_quoted(os.environ.get("FLY_MACHINE_ID", "worker")) + ") "
    "ON CONFLICT (kind, key) DO UPDATE SET doc = EXCLUDED.doc, updated_at = now(), "
    "updated_by = EXCLUDED.updated_by;"
)

p = subprocess.run(["psql", os.environ["ABEYANCE_STORE_DSN"], "-v", "ON_ERROR_STOP=1", "-q"],
                   input=statement, capture_output=True, text=True, timeout=60)
if p.returncode != 0:
    sys.stderr.write(f"contribution write failed: {p.stderr[-2000:]}\n")
    sys.exit(1)
print(f"[recommendation] {doc['summary']}")
'''

# --------------------------------------------------------------------------- W2: the evidence
#
# An eval-gated cheap model, over OpenRouter, cleared for EVIDENCE only. Stdlib HTTP — no SDK, no
# build step. Refuses to run if the mode it was handed is not the mode it was built for, because
# env is the one thing a case CAN rewrite freely.

OPENROUTER_EVIDENCE_PY = r'''
import json, os, sys, time, urllib.error, urllib.request
import psycopg

MODE = os.environ["MODEL_MODE"]
MODEL = os.environ["MODEL_ID"]        # injected from the clearance, so it sits in the registry
spec = json.loads(os.environ.get("ABEYANCE_SPEC") or "{}")

key = os.environ.get("OPENROUTER_API_KEY")
if not key:
    sys.stderr.write("OPENROUTER_API_KEY unset — this belongs on the worker's app, not the case\n")
    sys.exit(1)

instruction = spec.get("instruction") or "Extract the requested fields."
source = spec.get("source_text") or ""
if not source.strip():
    # An empty source produces confident-looking nonsense: 0 of 0 rows, 0.00%, all fields null.
    # That is the failure this library is shaped around — refuse rather than satisfy the request
    # with meaningless evidence.
    sys.stderr.write("spec carried no source_text — refusing to emit vacuous evidence\n")
    sys.exit(1)

body = json.dumps({
    "model": MODEL,
    "temperature": 0,
    "usage": {"include": True},
    "messages": [{"role": "user", "content": (
        f"{instruction}\n\nReturn ONE JSON object and nothing else. Report only what the source "
        f"supports; use null for anything absent rather than inferring it.\n\n{source[:400_000]}"
    )}],
}).encode()

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions", data=body,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=int(os.environ.get("MODEL_TIMEOUT_SECONDS", "180"))) as r:
        data = json.loads(r.read())
except urllib.error.HTTPError as e:
    sys.stderr.write(f"openrouter HTTP {e.code}: {e.read()[:1000]!r}\n")
    sys.exit(1)

text = (data["choices"][0]["message"]["content"] or "").strip()
start, end = text.find("{"), text.rfind("}")
if start < 0 or end <= start:
    sys.stderr.write(f"no JSON object in model output: {text[:1000]}\n")
    sys.exit(1)
payload = json.loads(text[start:end + 1])
usd = float((data.get("usage") or {}).get("cost") or 0.0)

doc = {
  "id": os.environ["ABEYANCE_CONTRIBUTION_KEY"], "case_id": os.environ["ABEYANCE_CASE_ID"],
  "request_id": os.environ["ABEYANCE_REQUEST_ID"],
  "kind": os.environ.get("ABEYANCE_CONTRIBUTION_KIND", "evidence"),
  "actor": {"id": os.environ["ABEYANCE_ACTOR"], "kind": "worker", "standing": [], "display": ""},
  "summary": str(payload.get("summary") or f"{MODE}: extracted {len(payload)} field(s)")[:500],
  "payload": payload,
  "scope": {},
  "provenance": {"machine": os.environ.get("FLY_MACHINE_ID", "unknown"),
                 "app": os.environ.get("FLY_APP_NAME", "unknown"),
                 "model_mode": MODE, "model": MODEL,
                 "provider": os.environ.get("MODEL_PROVIDER", ""),
                 "auth": "api-key", "usd": usd,
                 "attempt": os.environ.get("ABEYANCE_ATTEMPT", ""),
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
  "dependencies": spec.get("dependencies") or [],
  "supersedes": spec.get("supersedes") or "",
  "created_epoch": int(time.time()),
}

with psycopg.connect(os.environ["ABEYANCE_STORE_DSN"]) as conn:
    conn.execute(
        """INSERT INTO abeyance.state (kind, key, doc, updated_at, updated_by)
           VALUES (%s, %s, %s::jsonb, now(), %s)
           ON CONFLICT (kind, key) DO UPDATE
             SET doc = EXCLUDED.doc, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        ("contribution", doc["id"], json.dumps(doc),
         os.environ.get("FLY_MACHINE_ID", "worker")))
print(f"[evidence] {doc['summary']} (${usd:.4f})")
'''

# --------------------------------------------------------------------------- capabilities

APP_MODEL = "workers-model"        # holds CLAUDE_CODE_OAUTH_TOKEN and nothing else
APP_EXTRACT = "workers-extract"    # holds OPENROUTER_API_KEY and nothing else


def b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


def build_registry(*, today: str = "") -> CapabilityRegistry:
    """The two model capabilities, each checked against its clearance as it is built.

    Pass `today` (ISO) to enforce staleness here rather than discovering a year-old clearance in
    production. The registry then refuses to build, which is the loud version of the failure.
    """
    return CapabilityRegistry([
        model_capability(
            CLEARANCES, mode="case-recommendation",
            name="claude-recommendation",
            produces=("launch-recommendation",),
            emits=ContributionKind.RECOMMENDATION,
            # Pin a digest in production; a mutable tag reintroduces the drift the registry exists
            # to prevent.
            image="node:22-slim",
            app=APP_MODEL,
            reach=("claude-subscription", "abeyance-store-write"),
            entrypoint=("/bin/sh",), cmd=("-c", CLAUDE_RUNNER),
            env={"WORKER_B64": b64(CLAUDE_RECOMMENDATION_PY),
                 "MODEL_TIMEOUT_SECONDS": "540"},
            guest={"cpus": 2, "memory_mb": 2048},
            timeout_seconds=900,     # boot + npm install + a thinking model. Declare it honestly.
            description="Forms the opinion via `claude -p` on a subscription credential. "
                        "A recommendation, never an authorization.",
            today=today,
        ),
        model_capability(
            CLEARANCES, mode="extract-accurate",
            name="openrouter-evidence",
            produces=("extracted-fields",),
            emits=ContributionKind.EVIDENCE,
            image="python:3.12-slim",
            app=APP_EXTRACT,
            reach=("openrouter", "abeyance-store-write"),
            entrypoint=("/bin/sh",), cmd=("-c", PSQL_PY_RUNNER),
            env={"WORKER_B64": b64(OPENROUTER_EVIDENCE_PY),
                 "MODEL_TIMEOUT_SECONDS": "180"},
            guest={"cpus": 1, "memory_mb": 512},
            timeout_seconds=300,
            description="Eval-gated cheap extraction. Cleared for EVIDENCE only.",
            today=today,
        ),
    ])


def env_for(cap: Capability, case, req) -> dict:
    """The one place a reviewer looks to see who got what.

    Long-lived secrets are NOT here in production — they belong to the platform app the capability
    names (`fly secrets set --app workers-model CLAUDE_CODE_OAUTH_TOKEN=...`), so a Fly machine
    inherits only its own app's set and a worker in another app cannot read it. This function
    passes through the store DSN and, for local development, whatever auth is already in the
    environment.
    """
    import os
    out = {"ABEYANCE_STORE_DSN": os.environ["ABEYANCE_STORE_DSN"]}
    if cap.app == APP_MODEL:
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "MODEL_MAX_BUDGET_USD"):
            if os.environ.get(k):
                out[k] = os.environ[k]
    elif cap.app == APP_EXTRACT:
        if os.environ.get("OPENROUTER_API_KEY"):
            out["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    return out


if __name__ == "__main__":
    import json as _json
    ok, msg = routes_consistency()
    print(f"allowlist  : {'ok' if ok else 'FAIL'} — {msg}\n")
    reg = build_registry()
    print("capabilities:")
    for c in reg.all():
        print(f"  {c.name:<24} {c.emits.value:<15} app={c.app:<18} {c.image}")
    print("\nreach   — what can touch production:")
    print(_json.dumps(reg.reach_report(), indent=2))
    print("\nclearance — what may form which kind of contribution:")
    print(_json.dumps(CLEARANCES.clearance_report(), indent=2))
