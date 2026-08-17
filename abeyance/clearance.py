"""Which model may contribute what, and on what evidence.

Two properties get conflated, and they are independent:

- **Eval-gating** makes a recommendation *trustworthy* — you know, from a scored run against
  real data, how good this model is at this specific task.
- **Standing** makes a recommendation *non-authoritative* — it cannot become an action, at any
  accuracy. That is [`standing.py`](standing.py), and it is the half this library enforces.

With only the first you ship the model's opinion because the model is usually right, and on the
day it is wrong there is nothing underneath it. With only the second you are safe but blind: a
60%-accurate recommender and a 98%-accurate one are treated identically, so the human on the
thread carries all of the discrimination.

This module is the join, and the join is **per contribution kind**. A cheap model cleared by
eval for structured extraction is a fine source of EVIDENCE and an unacceptable source of a
RECOMMENDATION on an irreversible action — and that is a property of the *kind*, not of the
model's average quality. So a clearance names the kinds it covers:

    CLEARANCES = ClearanceRegistry([
        ModelClearance(mode="case-recommendation", model="claude-opus-5",
                       emits=(ContributionKind.RECOMMENDATION,),
                       evidence_ref="evals/launch-fit.md", verified_date="2026-08-17"),
        ModelClearance(mode="extract-accurate", model="deepseek/deepseek-v4-flash",
                       emits=(ContributionKind.EVIDENCE,),
                       evidence_ref="OBSERVATIONS.md Eval 1b", verified_date="2026-06-04"),
    ])

    CLEARANCES.require("extract-accurate", ContributionKind.EVIDENCE)        # -> the clearance
    CLEARANCES.require("extract-accurate", ContributionKind.RECOMMENDATION)  # -> NotCleared

Deliberately NOT here: the eval harness, the scoring, the API call. Those belong in whatever
picks your model. What belongs here is the *declaration* — small enough to review in a diff, and
strict enough that a mis-kinded capability fails at construction rather than in production.

A mode is named for its **use case**, never for its model. `extract-accurate` survives a model
swap; `deepseek-flash` becomes a lie the day you re-route it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .capability import Capability
from .errors import ConfigurationError, NotCleared
from .models import ContributionKind

DEFAULT_STALE_AFTER_DAYS = 120
"""A clearance is a measurement, and measurements age. Models get re-pointed, providers
re-quantize, and the eval that cleared a mode six months ago may no longer describe the thing
your request actually reaches. 120 days is a default, not a discovery — set it per clearance."""


@dataclass(frozen=True)
class ModelClearance:
    """One recorded verdict: this model, cleared for these contribution kinds, on this evidence.

    Every field is load-bearing for review. `evidence_ref` is required because a clearance
    without a recorded eval is an opinion wearing a data structure — if you cannot point at the
    run that cleared it, you have not cleared it.
    """

    mode: str
    """The use case, not the model. Consumers ask for a mode; the model behind it can change."""

    model: str
    """What the mode currently routes to. In the registry, therefore in git, therefore in the
    diff when it changes — which is the only reason a reviewer would ever notice a swap."""

    emits: Sequence[ContributionKind] = ()
    """The contribution kinds this clearance covers. An eval clears a model for a *task*, and a
    task produces one kind of thing; clearing `extract-accurate` for extraction says nothing
    about its judgment on a launch decision. Never contains DECISION — see `__post_init__`."""

    evidence_ref: str = ""
    """Where the eval is recorded. A file, a section, a URL — anything a reviewer can open."""

    verified_date: str = ""
    """ISO `YYYY-MM-DD`, the day the eval was scored. Not the day it was written down."""

    provider: str = ""
    """The endpoint the eval was scored on, when it matters. Two providers serving the "same"
    model at different quantizations are two different models as far as a score is concerned,
    so a clearance that does not pin the endpoint describes something you may not be calling."""

    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS

    notes: str = ""
    """Ideally the `do_not_use_when` half. A clearance that only says where a model is good is
    the half of the finding that cannot stop a misuse."""

    def __post_init__(self) -> None:
        if not self.mode:
            raise ConfigurationError("a clearance needs a mode")
        if not self.model:
            raise ConfigurationError(f"clearance {self.mode!r} names no model")
        if not self.emits:
            raise ConfigurationError(
                f"clearance {self.mode!r} covers no contribution kind — an eval clears a model "
                "for a task, and the task determines the kind. Name at least one.")
        if ContributionKind.DECISION in self.emits:
            raise ConfigurationError(
                f"clearance {self.mode!r} claims DECISION — no eval result can grant authority. "
                "Authority comes from a contribution's kind and the actor's standing "
                "(see standing.py); a model is cleared to be *good*, never to be *in charge*.")
        if not self.evidence_ref:
            raise ConfigurationError(
                f"clearance {self.mode!r} cites no evidence — an unrecorded clearance is an "
                "opinion. Point `evidence_ref` at the run that cleared it.")
        if not self.verified_date:
            raise ConfigurationError(
                f"clearance {self.mode!r} has no verified_date, so staleness cannot be "
                "detected and the clearance can never expire")
        try:
            date.fromisoformat(self.verified_date)
        except ValueError as e:
            raise ConfigurationError(
                f"clearance {self.mode!r} verified_date {self.verified_date!r} is not "
                f"ISO YYYY-MM-DD: {e}") from e
        if self.stale_after_days <= 0:
            raise ConfigurationError(
                f"clearance {self.mode!r} has stale_after_days={self.stale_after_days} — a "
                "clearance that is stale on arrival cannot be used at all")

    # ----------------------------------------------------------------- reading

    def covers(self, kind: ContributionKind) -> bool:
        return kind in tuple(self.emits)

    def age_days(self, today: str) -> int:
        """Days between the eval and `today` (ISO). Negative if the eval is dated ahead."""
        return (date.fromisoformat(today) - date.fromisoformat(self.verified_date)).days

    def is_stale(self, today: str) -> bool:
        return self.age_days(today) > self.stale_after_days

    def to_doc(self) -> Dict[str, Any]:
        return {"mode": self.mode, "model": self.model,
                "emits": [k.value for k in self.emits],
                "evidence_ref": self.evidence_ref, "verified_date": self.verified_date,
                "provider": self.provider, "stale_after_days": self.stale_after_days,
                "notes": self.notes}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "ModelClearance":
        return cls(mode=d["mode"], model=d["model"],
                   emits=tuple(ContributionKind(k) for k in (d.get("emits") or ())),
                   evidence_ref=d.get("evidence_ref", ""),
                   verified_date=d.get("verified_date", ""),
                   provider=d.get("provider", ""),
                   stale_after_days=int(d.get("stale_after_days") or DEFAULT_STALE_AFTER_DAYS),
                   notes=d.get("notes", ""))


class ClearanceRegistry:
    """The set of model clearances that exist. A small file, reviewable in a diff.

    Same shape and same reasoning as `CapabilityRegistry`: enumerable by intent, never by
    scanning for whatever models happen to be reachable. The review is the product.
    """

    def __init__(self, clearances: Iterable[ModelClearance] = (),
                 retired: Optional[Dict[str, str]] = None) -> None:
        self._by_mode: Dict[str, ModelClearance] = {}
        self.retired: Dict[str, str] = dict(retired or {})
        """`{mode: why}` for modes that were cleared once and are not any more.

        Kept rather than deleted so `require()` can refuse by name *with the reason*. An unknown
        mode and one disqualified for cause are different facts, and collapsing them into "unknown
        mode" is how something that failed an eval gets quietly re-added by someone who never saw
        the result."""
        for c in clearances:
            self.add(c)

    def add(self, clearance: ModelClearance) -> "ClearanceRegistry":
        if clearance.mode in self._by_mode:
            raise ConfigurationError(f"clearance {clearance.mode!r} is already registered")
        if clearance.mode in self.retired:
            raise ConfigurationError(
                f"clearance {clearance.mode!r} is also listed as retired ({self.retired[clearance.mode]}) "
                "— a mode cannot be simultaneously cleared and disqualified. Remove it from one.")
        self._by_mode[clearance.mode] = clearance
        return self

    def get(self, mode: str) -> Optional[ModelClearance]:
        return self._by_mode.get(mode)

    def modes(self) -> List[str]:
        return sorted(self._by_mode)

    def all(self) -> List[ModelClearance]:
        return [self._by_mode[m] for m in self.modes()]

    # ----------------------------------------------------------------- the gate

    def require(self, mode: str, kind: ContributionKind, *, today: str = "") -> ModelClearance:
        """The clearance for `mode`, or raise `NotCleared`.

        `today` (ISO) is optional and opts into the staleness check — this module holds no clock,
        so a caller that wants expiry enforced passes the date it already has. Omitting it is a
        deliberate choice to trust the declaration, not an oversight the module papers over.
        """
        if mode in self.retired:
            raise NotCleared(
                f"mode {mode!r} was retired: {self.retired[mode]} Retired modes are refused by "
                f"name, not forgotten — re-clearing one needs a passing eval.")
        c = self._by_mode.get(mode)
        if c is None:
            raise NotCleared(
                f"model mode {mode!r} is not on the clearance registry. Cleared modes: "
                f"{self.modes()}. A new mode needs a recorded eval, not a registry entry.")
        if not c.covers(kind):
            raise NotCleared(
                f"mode {mode!r} ({c.model}) is cleared for "
                f"{sorted(k.value for k in c.emits)}, not {kind.value!r}. Being good at one "
                f"task is not evidence about another — clear it for {kind.value!r} with its own "
                f"eval, or route this need to a mode that already is.")
        if today and c.is_stale(today):
            raise NotCleared(
                f"mode {mode!r} was verified {c.verified_date} ({c.age_days(today)}d ago, "
                f"limit {c.stale_after_days}d) and is stale. Re-run its eval and update "
                f"verified_date; a clearance describes what was true when it was measured.")
        return c

    # ----------------------------------------------------------------- audit

    def clearance_report(self) -> Dict[str, List[str]]:
        """`{kind: [modes]}` — "what is allowed to form an opinion?" in one call.

        The counterpart to `CapabilityRegistry.reach_report()`: that one answers what can touch
        production, this one answers what can influence a decision.
        """
        out: Dict[str, List[str]] = {}
        for c in self.all():
            for k in c.emits:
                out.setdefault(k.value, []).append(c.mode)
        return {k: sorted(v) for k, v in sorted(out.items())}

    def stale(self, today: str) -> List[ModelClearance]:
        """Every clearance past its own limit. Sweep this; do not wait to be surprised."""
        return [c for c in self.all() if c.is_stale(today)]

    def to_doc(self) -> Dict[str, Any]:
        return {"clearances": [c.to_doc() for c in self.all()]}

    def __len__(self) -> int:
        return len(self._by_mode)

    def __contains__(self, mode: object) -> bool:
        return mode in self._by_mode

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ClearanceRegistry {self.modes()}>"


# --------------------------------------------------------------------------- wiring


def model_capability(clearances: ClearanceRegistry, *, mode: str, name: str,
                     produces: Sequence[str], emits: ContributionKind,
                     image: str, app: str, reach: Sequence[str] = (),
                     env: Optional[Dict[str, str]] = None, today: str = "",
                     **kw: Any) -> Capability:
    """Build a `Capability` whose worker calls a model, refusing an un-cleared kind.

    The check runs at *construction* — registry build time, not dispatch time — so a capability
    that would emit a kind its model was never cleared for cannot be registered at all. A gate
    that only fires when the container is already running is a gate you find out about from a
    contribution you should not have trusted.

    The clearance's `model` and `provider` are injected into the worker's static env, which puts
    them in the registry and therefore in the diff. That is the point: a silent model swap
    becomes a reviewable line change.
    """
    c = clearances.require(mode, emits, today=today)
    merged = {"MODEL_MODE": c.mode, "MODEL_ID": c.model, **(env or {})}
    if c.provider:
        merged["MODEL_PROVIDER"] = c.provider
    return Capability(name=name, image=image, produces=tuple(produces), emits=emits,
                      reach=tuple(reach), app=app, env=merged, **kw)


# --------------------------------------------------------------------------- loading a declaration


def unmapped_modes(spec: Dict[str, Any], kind_map: Dict[str, Sequence[ContributionKind]]
                   ) -> List[str]:
    """Modes present in the allowlist with no contribution kind assigned. Report these."""
    return sorted(set((spec.get("modes") or {})) - set(kind_map))


def from_allowlist(spec: Dict[str, Any],
                   kind_map: Dict[str, Sequence[ContributionKind]],
                   *, default_stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
                   ) -> ClearanceRegistry:
    """Turn an eval-gated model allowlist into a `ClearanceRegistry`.

    Provider-agnostic: nothing here knows about any particular routing vendor. The input is any
    dict of the shape

        {"staleness_warn_days": 120,                         # optional
         "modes": {"<mode>": {"model": "...",                 # required
                              "verified_date": "YYYY-MM-DD",  # required
                              "evidence_ref": "...",          # strongly recommended
                              "provider": "..." | {"order": [...]},
                              "do_not_use_when": "...",       # or "purpose"
                              "stale_after_days": 90}},
         "retired": {"<mode>": "why it was disqualified"}}    # optional

    which is what most routing allowlists already look like — so the file you already maintain
    stays the single source of truth and this reads it rather than duplicating it.

    `kind_map` is deliberately a separate argument, and deliberately hand-written: **a mode absent
    from it gets no clearance.** Adding a routing mode must not silently grant it a contribution
    kind — somebody has to decide whether a new model produces facts or opinions, and that decision
    should appear in a diff next to its reasoning. Call `unmapped_modes()` to find omissions.
    """
    default_stale = int(spec.get("staleness_warn_days") or default_stale_after_days)

    retired_raw = spec.get("retired") or {}
    retired = {m: (v if isinstance(v, str) else str((v or {}).get("reason") or "retired"))
               for m, v in retired_raw.items()}

    reg = ClearanceRegistry(retired=retired)
    for mode, entry in (spec.get("modes") or {}).items():
        kinds = tuple(kind_map.get(mode) or ())
        if not kinds:
            continue
        provider = entry.get("provider")
        if isinstance(provider, dict):
            order = provider.get("order") or [""]
            provider = order[0] if order else ""
        notes = entry.get("do_not_use_when") or entry.get("purpose") or ""
        reg.add(ModelClearance(
            mode=mode,
            model=entry["model"],
            emits=kinds,
            evidence_ref=entry.get("evidence_ref") or "",
            verified_date=entry["verified_date"],
            provider=provider or "",
            stale_after_days=int(entry.get("stale_after_days") or default_stale),
            notes=f"do_not_use_when: {notes}" if notes else "",
        ))
    return reg
