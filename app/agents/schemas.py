"""What each agent must hand back. The JSON schemas of these models go to the model as the
shape of its final answer; the models then validate what comes back."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceIds = list[str]
Kind = Literal["fact", "inference", "hypothesis"]
Signal = Literal["metrics", "logs", "traces", "changes", "flags"]
INVESTIGATORS = ("metrics", "logs_traces", "changes")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Briefs(_Model):
    metrics: str = Field(max_length=600, description="What the metrics investigator should establish first")
    logs_traces: str = Field(max_length=600, description="What the logs and traces investigator should look for")
    changes: str = Field(max_length=600, description="What the changes investigator should check")


class Plan(_Model):
    """The coordinator's answer."""

    summary: str = Field(max_length=600, description="What is known from the alerts, in two or three sentences")
    hypotheses: list[str] = Field(max_length=5, description="Candidate explanations worth checking, most likely first")
    briefs: Briefs


class Finding(_Model):
    statement: str = Field(max_length=500, description="One specific, checkable claim with the numbers that support it")
    kind: Kind = Field(
        description="fact: the cited data shows it directly; inference: follows from the cited data with high "
        "confidence; hypothesis: plausible but not shown"
    )
    signal: Signal = Field(description="Which kind of data the claim rests on")
    service: str | None = Field(None, max_length=64, description="The service the claim is about (service_name)")
    evidence_ids: EvidenceIds = Field(
        max_length=6, description="ev_... ids from your own tool results; required for facts and inferences"
    )
    confidence: float = Field(ge=0, le=1)


class InvestigatorReport(_Model):
    """An investigator's answer."""

    summary: str = Field(max_length=600, description="What your data shows, in two or three sentences")
    findings: list[Finding] = Field(max_length=8)


class RootCause(_Model):
    service: str = Field(
        max_length=64,
        description="Where the cause is: the service to fix (service_name), not one that only shows symptoms",
    )
    cause: str = Field(max_length=500, description="What is wrong there, specifically")
    confidence: float = Field(ge=0, le=1)
    evidence_ids: EvidenceIds = Field(min_length=1, max_length=8)


class Claim(_Model):
    statement: str = Field(max_length=500)
    evidence_ids: EvidenceIds = Field(min_length=1, max_length=8)


class OpenQuestion(_Model):
    statement: str = Field(max_length=500)
    how_to_verify: str = Field(max_length=300)


class Diagnosis(_Model):
    """The synthesizer's answer."""

    summary: str = Field(max_length=800, description="What happened and why, for the on-call engineer")
    # An empty list is allowed: "the evidence does not point anywhere" escalates to a human.
    root_causes: list[RootCause] = Field(max_length=3, description="Ranked candidates, most likely first")
    facts: list[Claim] = Field(max_length=10, description="What the evidence shows directly")
    inferences: list[Claim] = Field(max_length=6, description="High-confidence conclusions drawn from the facts")
    hypotheses: list[OpenQuestion] = Field(max_length=5, description="Plausible but unverified; how to check each")

    def cited(self) -> set[str]:
        return {
            ev
            for items in (self.root_causes, self.facts, self.inferences)
            for item in items
            for ev in item.evidence_ids
        }
