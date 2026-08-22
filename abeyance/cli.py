"""`abeyance` — drive a loop from a shell, a cron entry, or a container.

Every subcommand prints JSON to stdout and diagnostics to stderr, so a scheduled runner can
pipe it into `jq` and branch on the result without parsing prose. The split that matters is
between the free commands and the expensive one:

    abeyance --app app:loop poll        # deterministic, no model, safe to run hourly
    abeyance --app app:loop read  --id T   # transport reads; still no model
    abeyance --app app:loop record --id T --from a@b --approve 1,3
    abeyance --app app:loop apply --id T --executor app:run

A shell runner should call `poll` first and exit when it prints an empty `actionable` list.
That is the whole cost argument for the abeyance model, and putting it in the CLI makes the
cheap path the obvious one to wire up.

Exit codes are categorical so a runner can branch without reading the body:
    0 fine · 2 usage/config · 3 not found · 4 blocked (waiting on approvers) · 5 transport
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from .errors import (AlreadyExecuted, CaseNotFound, ConfigurationError, AbeyanceError,
                     NotAuthorized, ProposalNotFound, TransportError, UnknownApprover)
from .loop import ApprovalLoop

if TYPE_CHECKING:  # pragma: no cover - import cost stays off the hot path
    from .cases import CaseLoop

EXIT_OK, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_BLOCKED, EXIT_TRANSPORT = 0, 2, 3, 4, 5


def _resolve(spec: str) -> Any:
    """`package.module:attribute` -> the object. Calls it if it is a zero-arg factory."""
    if ":" not in spec:
        raise ConfigurationError(f"expected 'module:attribute', got {spec!r}")
    mod_name, attr = spec.split(":", 1)
    sys.path.insert(0, "")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        raise ConfigurationError(f"cannot import {mod_name!r}: {e}") from e
    try:
        obj = getattr(mod, attr)
    except AttributeError as e:
        raise ConfigurationError(f"{mod_name!r} has no attribute {attr!r}") from e
    return obj


def _load_loop(spec: str) -> ApprovalLoop:
    obj = _resolve(spec)
    if isinstance(obj, ApprovalLoop):
        return obj
    if callable(obj):
        built = obj()
        if isinstance(built, ApprovalLoop):
            return built
    raise ConfigurationError(f"{spec!r} is not an ApprovalLoop (or a factory returning one)")


def _nums(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    return sorted({int(x) for x in raw.replace(";", ",").replace(" ", ",").split(",") if x.strip()})


def _out(obj: Any, code: int = EXIT_OK) -> int:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    return code


# --------------------------------------------------------------------------- commands


def cmd_pending(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    rows = []
    for p in loop.pending():
        s = loop.summary(p)
        rows.append({"id": p.id, "subject_key": p.subject_key, "track": p.track,
                     "status": p.status.value, "turns": p.turns,
                     "items": len(p.actionable_items), "waiting_on": p.waiting_on,
                     "verdicts": s.to_doc()})
    return _out(rows)


def cmd_poll(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    res = loop.poll(expire=not a.no_expire)
    return _out(res.to_doc(), EXIT_OK if res.actionable else EXIT_OK)


def cmd_read(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    inbound = loop.read(a.id)
    p = loop.get(a.id)
    return _out({"id": a.id, "subject_key": p.subject_key, "found": len(inbound),
                 "still_waiting": p.waiting_on,
                 "replies": [i.to_doc() for i in inbound],
                 "note": ("The suggestion is a hint, not the judge. Read each reply and call "
                          "`record` with what the person actually meant — a conditional or "
                          "reworded reply is not an approval.")})


def cmd_record(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    p = loop.record(a.id, a.sender, approve=_nums(a.approve), reject=_nums(a.reject),
                    raw=a.raw, reply_ids=[x for x in (a.reply_ids or "").split(",") if x])
    s = loop.summary(p)
    return _out({"id": p.id, "recorded_for": a.sender.lower(), "status": p.status.value,
                 "all_replied": not p.waiting_on, "waiting_on": p.waiting_on,
                 "verdicts": s.to_doc()})


def cmd_apply(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    executor = _resolve(a.executor) if a.executor else (lambda item: {"written": True})
    report = loop.execute(a.id, executor, dry_run=a.dry_run, force=a.force)
    code = EXIT_BLOCKED if report.blocked else EXIT_OK
    return _out(report.to_doc(), code)


def cmd_nudge(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    return _out(loop.nudge(proposal_id=a.id, dry_run=a.dry_run).to_doc())


def cmd_sweep(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    return _out(loop.sweep())


def cmd_show(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    p = loop.get(a.id)
    return _out({**p.to_doc(), "verdicts": loop.summary(p).to_doc()})


def cmd_dismiss(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    p = loop.dismiss(a.id, [x for x in (a.reply_ids or "").split(",") if x], note=a.note or "")
    return _out({"id": p.id, "seen_reply_ids": p.seen_reply_ids})


def cmd_ask(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    p = loop.ask(a.id, a.text)
    if p is None:
        return _out({"id": a.id, "stalled": True,
                     "reason": "turn cap reached — escalated instead of asking again"})
    return _out({"id": p.id, "turns": p.turns, "status": p.status.value})


def cmd_inject(loop: ApprovalLoop, a: argparse.Namespace) -> int:
    """Drive the state machine with no mailbox and no humans.

    Only works on a transport that can accept a synthetic inbound (`MemoryTransport`). It is
    here because the multi-approver paths — a split verdict, a second approver arriving a day
    later — are the ones worth rehearsing before real people are on the thread, and rehearsing
    them should not require real people.
    """
    receive = getattr(loop.transport, "receive", None)
    if receive is None:
        raise ConfigurationError(
            f"{type(loop.transport).__name__} cannot accept an injected reply — use "
            "MemoryTransport for rehearsals")
    receive(a.id, a.sender, a.text)
    return _out({"id": a.id, "injected_from": a.sender.lower(),
                 "replies": [i.to_doc() for i in loop.read(a.id)]})


# --------------------------------------------------------------------------- cases
#
# The case-layer subcommands. `--app` resolves to a CaseLoop for these instead of an
# ApprovalLoop; `main()` picks the loader from the subcommand's `needs` default.
#
# `tick` is the case layer's equivalent of `poll` — the cheap, deterministic, model-free thing a
# cron entry runs, and the one that should be wired up first.


def _load_cases(spec: str) -> "CaseLoop":
    from .cases import CaseLoop
    obj = _resolve(spec)
    if isinstance(obj, CaseLoop):
        return obj
    if callable(obj):
        built = obj()
        if isinstance(built, CaseLoop):
            return built
    raise ConfigurationError(f"{spec!r} is not a CaseLoop (or a factory returning one)")


def cmd_case_tick(cases: "CaseLoop", a: argparse.Namespace) -> int:
    """THE CHEAP GATE. Deterministic: collect, derive, dispatch, authorize. No model calls."""
    standing = json.loads(a.standing) if a.standing else None
    reports = cases.tick(a.id or None, harvest_standing=standing)
    docs = [r.to_doc() for r in reports]
    actionable = [r.case_id for r in reports if r.actionable]
    return _out({"ticked": len(docs), "actionable": actionable, "cases": docs})


def cmd_case_open(cases: "CaseLoop", a: argparse.Namespace) -> int:
    case = cases.open(action=a.action, subject_key=a.subject_key or "", title=a.title or "",
                      needs=_csv(a.needs),
                      context=json.loads(a.context) if a.context else None)
    return _out(case.to_doc())


def cmd_case_show(cases: "CaseLoop", a: argparse.Namespace) -> int:
    case = cases.get(a.id)
    auth = cases.authority(a.id)
    return _out({"case": case.to_doc(),
                 "contributions": [c.to_doc() for c in cases.contributions(a.id)],
                 "authority": auth.to_doc(),
                 "summary": cases.summary(a.id)})


def cmd_case_authority(cases: "CaseLoop", a: argparse.Namespace) -> int:
    auth = cases.authority(a.id)
    return _out(auth.to_doc(), EXIT_OK if auth.granted else EXIT_BLOCKED)


def cmd_case_contribute(cases: "CaseLoop", a: argparse.Namespace) -> int:
    """The worker-facing command, for a worker that would rather not write SQL.

    Note what it will not do: `--kind decision` is refused. Decisions come from the approval
    layer, where a reply is attributed to a sender by the transport.
    """
    from .models import Actor, ContributionKind
    c = cases.contribute(
        a.id, kind=ContributionKind(a.kind), actor=Actor.worker(a.actor),
        request_id=a.request, summary=a.summary or "",
        payload=json.loads(a.payload) if a.payload else None,
        scope=json.loads(a.scope) if a.scope else None,
        provenance=json.loads(a.provenance) if a.provenance else None,
        dependencies=_csv(a.dependencies), supersedes=a.supersedes or "")
    return _out(c.to_doc())


def cmd_case_execute(cases: "CaseLoop", a: argparse.Namespace) -> int:
    executor = _resolve(a.executor) if a.executor else (lambda case, auth, cs: None)
    out = cases.execute(a.id, executor, dry_run=a.dry_run)
    return _out(out.to_doc(), EXIT_BLOCKED if out.blocked or out.error else EXIT_OK)


def cmd_case_sweep(cases: "CaseLoop", a: argparse.Namespace) -> int:
    return _out(cases.sweep())


def cmd_case_plan(cases: "CaseLoop", a: argparse.Namespace) -> int:
    """What the planner proposed, what was accepted, and what it has left to spend.

    The command to reach for when a case looks like it is thinking instead of closing. It reads
    only — the review is a pure function of the plan, the case and the registry, so running this
    a year later prints what the tick that acted on it saw.
    """
    from .planner import planner_for
    from .warrant import CaseView
    planner = planner_for(cases.rules)
    if planner is None:
        return _out({"planner": None,
                     "note": "no Planner is wired into this case loop's rules"}, EXIT_USAGE)
    case = cases.get(a.id)
    view = CaseView(case, cases.contributions(a.id))
    return _out({"case": case.id, "status": case.status.value, **planner.status(view)})


def cmd_case_capabilities(cases: "CaseLoop", a: argparse.Namespace) -> int:
    """What workers exist and what each can reach. The registry, as a review artefact."""
    return _out({"capabilities": cases.registry.to_doc()["capabilities"],
                 "reach": cases.registry.reach_report(),
                 "rules": [{"name": r.name, "description": r.description} for r in cases.rules]})


def _csv(raw: Optional[str]) -> List[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="abeyance", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", required=True, metavar="module:attr",
                    help="an ApprovalLoop (or CaseLoop, for the case-* commands), or a "
                         "zero-arg factory returning one")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pending", help="open proposals and their verdicts").set_defaults(
        func=cmd_pending)

    p = sub.add_parser("poll", help="THE CHEAP GATE — is there anything worth waking for?")
    p.add_argument("--no-expire", action="store_true",
                   help="do not settle overdue proposals while polling")
    p.set_defaults(func=cmd_poll)

    r = sub.add_parser("read", help="fetch new replies + parse suggestions (records nothing)")
    r.add_argument("--id", required=True)
    r.set_defaults(func=cmd_read)

    rec = sub.add_parser("record", help="write one approver's decision")
    rec.add_argument("--id", required=True)
    rec.add_argument("--from", dest="sender", required=True)
    rec.add_argument("--approve", default="", help='item numbers, e.g. "1,3"')
    rec.add_argument("--reject", default="")
    rec.add_argument("--raw", help="the reply verbatim, for the audit trail")
    rec.add_argument("--reply-ids", help="comma-separated message ids this decision came from")
    rec.set_defaults(func=cmd_record)

    ap_ = sub.add_parser("apply", help="execute the items that cleared")
    ap_.add_argument("--id", required=True)
    ap_.add_argument("--executor", metavar="module:fn")
    ap_.add_argument("--dry-run", action="store_true")
    ap_.add_argument("--force", action="store_true",
                     help="stop waiting: run what is approved now, abandon the rest")
    ap_.set_defaults(func=cmd_apply)

    n = sub.add_parser("nudge", help="chase quiet approvers, on the policy's schedule")
    n.add_argument("--id")
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(func=cmd_nudge)

    sub.add_parser("sweep", help="settle expired and stalled proposals").set_defaults(
        func=cmd_sweep)

    s = sub.add_parser("show", help="one proposal, in full")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_show)

    d = sub.add_parser("dismiss", help="mark replies read without treating them as decisions")
    d.add_argument("--id", required=True)
    d.add_argument("--reply-ids", required=True)
    d.add_argument("--note")
    d.set_defaults(func=cmd_dismiss)

    q = sub.add_parser("ask", help="send a clarification on the thread (counts a turn)")
    q.add_argument("--id", required=True)
    q.add_argument("--text", required=True)
    q.set_defaults(func=cmd_ask)

    j = sub.add_parser("inject", help="REHEARSAL ONLY — fake an inbound reply")
    j.add_argument("--id", required=True)
    j.add_argument("--from", dest="sender", required=True)
    j.add_argument("--text", required=True)
    j.set_defaults(func=cmd_inject)

    # ---- case layer. `--app` must resolve to a CaseLoop for these. ----------

    ct = sub.add_parser("case-tick",
                        help="THE CHEAP GATE for cases — collect, derive, dispatch, authorize")
    ct.add_argument("--id", help="one case; omit for every open case")
    ct.add_argument("--standing", metavar="JSON",
                    help='{"a@b.com": ["launch-campaign"]} — who may decide what. Omit to skip '
                         "harvesting decisions this tick.")
    ct.set_defaults(func=cmd_case_tick, needs="case")

    co = sub.add_parser("case-open", help="open a case and record what it needs")
    co.add_argument("--action", required=True, help="the label standing is checked against")
    co.add_argument("--subject-key")
    co.add_argument("--title")
    co.add_argument("--needs", help="comma-separated need labels")
    co.add_argument("--context", metavar="JSON")
    co.set_defaults(func=cmd_case_open, needs="case")

    cs = sub.add_parser("case-show", help="one case, its contributions, and its authority")
    cs.add_argument("--id", required=True)
    cs.set_defaults(func=cmd_case_show, needs="case")

    ca = sub.add_parser("case-authority", help="may this be acted on now? exit 4 if not")
    ca.add_argument("--id", required=True)
    ca.set_defaults(func=cmd_case_authority, needs="case")

    cc = sub.add_parser("case-contribute", help="WORKER-FACING — write one contribution")
    cc.add_argument("--id", required=True, help="case id (ABEYANCE_CASE_ID)")
    cc.add_argument("--request", default="", help="request id (ABEYANCE_REQUEST_ID)")
    cc.add_argument("--kind", default="evidence", choices=["evidence", "recommendation"],
                    help="'decision' is deliberately not available here")
    cc.add_argument("--actor", required=True, help="capability name; recorded as worker:<name>")
    cc.add_argument("--summary")
    cc.add_argument("--payload", metavar="JSON")
    cc.add_argument("--scope", metavar="JSON")
    cc.add_argument("--provenance", metavar="JSON")
    cc.add_argument("--dependencies", help="comma-separated contribution ids")
    cc.add_argument("--supersedes")
    cc.set_defaults(func=cmd_case_contribute, needs="case")

    ce = sub.add_parser("case-execute", help="act under authority, re-checked at commit time")
    ce.add_argument("--id", required=True)
    ce.add_argument("--executor", metavar="module:fn")
    ce.add_argument("--dry-run", action="store_true")
    ce.set_defaults(func=cmd_case_execute, needs="case")

    sub.add_parser("case-sweep", help="expire cases nobody has contributed to").set_defaults(
        func=cmd_case_sweep, needs="case")

    cp = sub.add_parser("case-plan",
                        help="what the planner proposed, what was accepted, and what is left")
    cp.add_argument("--id", required=True)
    cp.set_defaults(func=cmd_case_plan, needs="case")

    sub.add_parser("case-capabilities",
                   help="what workers exist and what each can reach").set_defaults(
        func=cmd_case_capabilities, needs="case")

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        loop = (_load_cases(args.app) if getattr(args, "needs", "") == "case"
                else _load_loop(args.app))
        return args.func(loop, args)
    except CaseNotFound as e:
        print(str(e), file=sys.stderr)
        return EXIT_NOT_FOUND
    except NotAuthorized as e:
        print(f"not authorized: {e}", file=sys.stderr)
        return EXIT_BLOCKED
    except ConfigurationError as e:
        print(f"configuration: {e}", file=sys.stderr)
        return EXIT_USAGE
    except ProposalNotFound as e:
        print(str(e), file=sys.stderr)
        return EXIT_NOT_FOUND
    except (UnknownApprover, AlreadyExecuted, ValueError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    except TransportError as e:
        print(f"transport: {e}", file=sys.stderr)
        return EXIT_TRANSPORT
    except AbeyanceError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
