"""Ask once: the owner's answer to an unknown question becomes a cited fact for every later customer.

When the owner answers an `owner_question` that was not about a specific profile fact (the assistant
said "I don't know, want me to ask Rosa?"), `KnowledgeLearner.learn` writes a new profile revision:

  * a knowledge entry whose answer is the owner's own words (made speakable), asked as the customer
    asked it, with `owner_confirmed: true` and evidence `urn:askonce:owner-answer:<commitment id>`;
  * an `owner_answer` source for that URN;
  * an incremental certification run bound to the new revision's sha256 (invariant L7). It re-runs the
    checks the new fact can affect: the full loader (schema + L1-L11, which includes the injection
    canary scan of every profile string), an injection screen of the answer itself, and the grounding
    rule (the new answer is statable). If any check fails, nothing is published and the customer who
    asked still gets the owner's answer through their task as before.

The store write is a compare-and-swap on the revision the answer was learned from, so a concurrent
edit is never overwritten (the learner simply skips; the answer is still on the request).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from askonce_server import errors
from askonce_server.canonical import profile_sha256
from askonce_server.clock import Clock
from askonce_server.profile.loader import load_profile
from askonce_server.profile.model import Profile
from askonce_server.profile.store import ProfileStore
from askonce_server.tools import grounding
from askonce_server.tools.guard import sanitize_speak
from askonce_server.tools.render import OWNER_ANSWER_URN

logger = logging.getLogger(__name__)

LEARN_SUITE = {"id": "ask_once", "version": "1.0"}
_ADDRESSES_AI = re.compile(
    r"\b(ai|a\.i\.)\s+(agents?|assistants?)\b|\bignore (all |any )?(previous|prior|above) instructions\b|"
    r"\bsystem (prompt|notice)\b|\bchat ?bots?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Learned:
    status: str  # learned | skipped | rejected
    detail: str
    profile: Profile | None = None
    entry_ref: str | None = None


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:40].strip("-") or "answer"


def learned_document(
    doc: dict[str, Any], *, commitment_id: str, question: str, answer: str, answered_at: str
) -> tuple[dict[str, Any], str]:
    """The next revision of `doc` with the owner's answer as a knowledge entry. Returns (doc, entry ref)."""
    out = dict(doc)
    modules = dict(out["modules"])
    module_id = next((m for m, mod in modules.items() if mod.get("type") == "knowledge"), None)
    if module_id is None:
        module_id = "info" if "info" not in modules else "learned"
        modules[module_id] = {"type": "knowledge", "title": "Policies and FAQ", "entries": []}
    module = dict(modules[module_id])
    entries = list(module["entries"])
    base = f"asked-{_slug(question)}"[:60]
    entry_id, n = base, 2
    while any(e["id"] == entry_id for e in entries):
        entry_id, n = f"{base}-{n}"[:64], n + 1
    urn = f"{OWNER_ANSWER_URN}{commitment_id}"
    question_id = f"asked-{_slug(commitment_id)}"[:64]
    spoken = sanitize_speak(answer)
    entries.append({
        "id": entry_id,
        "topic": "owner_answer",
        "question_variants": [question[:120]],
        "answer_spoken": spoken,
        **({"answer_card": answer[:1000]} if answer.strip() != spoken else {}),
        "provenance": {"owner_confirmed": True, "evidence": [{"url": urn, "quote": answer[:500]}]},
    })
    module["entries"] = entries
    modules[module_id] = module
    out["modules"] = modules
    sources = dict(out["sources"])
    sources[f"owner-answer-{_slug(commitment_id)}"[:64]] = {
        "kind": "owner_answer",
        "url": urn,
        "title": "Owner's answer"[:120],
        "retrieved_at": answered_at,
        "question_id": question_id,
    }
    out["sources"] = sources
    # The question the source answers (L1: an owner_answer source names its question).
    out["open_questions"] = [*doc.get("open_questions", []), {
        "id": question_id,
        "kind": "missing",
        "targets": [f"{module_id}/{entry_id}#answer_spoken"],
        "prompt": question[:300],
        "why": "A customer asked this and the website does not say."[:300],
        "blocking": False,
        "status": "answered",
        "answer": {"value": answer[:1000], "raw_text": answer[:1000], "answered_at": answered_at},
    }]
    out["revision"] = int(doc["revision"]) + 1
    out["updated_at"] = answered_at
    return out, f"{module_id}/{entry_id}"


class KnowledgeLearner:
    def __init__(self, profiles: ProfileStore, *, schema_path: Path, clock: Clock) -> None:
        self.profiles = profiles
        self.schema_path = schema_path
        self.clock = clock

    async def learn(self, business_id: str, *, commitment_id: str, question: str | None, answer: str | None) -> Learned:
        if not question or not answer or not answer.strip():
            return Learned("skipped", "no question or answer")
        if _ADDRESSES_AI.search(answer) or _ADDRESSES_AI.search(question):
            return Learned("rejected", "the text addresses an AI assistant")
        current = await self.profiles.get(business_id)
        if current is None or not current.is_live:
            return Learned("skipped", "business is not live")
        now = self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        doc, entry_ref = learned_document(
            current.to_dict(), commitment_id=commitment_id, question=question, answer=answer.strip(), answered_at=now
        )
        # Incremental certification of the new revision.
        certification = dict(doc.get("certification") or {})
        doc["certification"] = {"status": "draft", "runs": list(certification.get("runs", []))}
        sha = profile_sha256(doc)
        run_id = f"learn-{doc['revision']}"
        run = {"id": run_id, "suite": dict(LEARN_SUITE), "profile_revision": doc["revision"], "profile_sha256": sha,
               "started_at": now, "finished_at": now, "result": "passed", "cases_total": 3, "cases_passed": 3, "failures": []}
        doc["certification"] = {"status": current.status, "certified_run": run_id, "runs": [*doc["certification"]["runs"], run]}
        try:
            loaded = load_profile(doc, schema_path=self.schema_path, strict=False)
        except errors.ProfileInvalid as exc:
            logger.warning("ask-once: answer for %s not learned: %s", business_id, exc)
            return Learned("rejected", f"the new revision does not validate: {exc}"[:300])
        if not grounding.state(loaded.profile, f"{entry_ref}#answer_spoken").ok:
            return Learned("rejected", "the learned answer is not statable")
        try:
            await self.profiles.put(loaded.profile, expected_revision=current.revision)
        except errors.ConcurrencyConflict:
            return Learned("skipped", "the profile changed meanwhile")
        logger.info("ask-once: %s revision %s learned %s", business_id, doc["revision"], entry_ref)
        return Learned("learned", "published", loaded.profile, entry_ref)
