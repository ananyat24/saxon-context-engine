# Regression Watchlist — Saxon Context Engine

Built from actual git history and CLAUDE.md's own running bug log, not
generic assumptions. Every item below is a **confirmed, previously real,
now-fixed bug** with a commit, unless marked "still open." For each fixed
bug: the shape of the regression, the fix, and the one question/interaction
that would expose it if it ever regressed.

---

## 1. Causal walker dead-ending on PRODUCES/TRIGGERED_BY edges

**History:** Found live against real Solandra data (2026-09, one of the
original 8 cross-source reasoning questions). The causal-chain walker
(`GraphRepository.causal_chain_for_query`) only follows relationship types
flagged `causal: true`. `PRODUCES` and `TRIGGERED_BY` weren't flagged, so a
real root-cause chain ("Ferrotek PRODUCES a lot, that lot PRODUCES a
defective component," "corrective action TRIGGERED_BY the defect") dead-ended
one hop short of the actual root cause even though the graph fully connected
it. Fixed in `ontology/core.yaml` (flag both `causal: true`) — commit
`3cf7fb2`. Confirmed closed in the 2026-09-03 audit (`a063f0d`). Covered by
`tests/test_causal_chain.py::test_a_produces_and_triggered_by_chain_is_now_walked`.

**Deliberately NOT flagged causal**, so don't "fix" this if it recurs:
`PROVIDES`/`PERFORMED_ON` (administrative, not causal) —
`tests/test_ontology.py::test_causal_relationship_types_includes_core_generic_types`
asserts they stay excluded.

**Regression probe:** *"Why is SO-45821 at risk?"* against `solandra_supply_chain`.
A healthy answer must reach all the way to Ferrotek Components / lot FT-6602 /
the ISO 9001 hold, not stop at "Quality event affects Plant 2." Confirmed live
in this session's baseline (`eval/baseline_pre_polish.json`, question 1) — the
full chain is present today.

---

## 2. `:Decision` entity-type collision hiding real client data

**History:** `ontology/core.yaml`'s generic `Decision` business-entity type
(approvals, budget decisions) collided with Saxon's own internal
causal-recommendation audit-trail nodes, which were *also* labeled
`:Decision`. Every retrieval path excluded `:Decision` to hide Saxon's own
audit trail — which also hid **real client data**: a genuine $38,400 budget
approval (`DEC-2026-014`) became permanently unreachable, even by its own
literal ID, the moment a client's `decisions.csv` got auto-ingested. Fixed by
giving Saxon's own generated nodes a second, narrower label
(`:SaxonRecommendation`) and filtering on that everywhere instead — commit
`3cf7fb2` / `8af92f9` (embedding fix for the new label) / `1cf224b` (backfill
migration for pre-fix nodes). Confirmed closed 2026-09-03.

**Regression probe:** Ingest (or confirm already-ingested) a client record
whose entity type resolves to `Decision`, then ask a query that names it
directly by ID or name — e.g. *"What is DEC-2026-014?"* or *"What decision
unblocked QE-2091, and who approved it?"* (this session's baseline question 2
confirms QE-2091's decision chain — Meridian Circuit Supply's qualification
lot, approved and shipped 2026-08-27 — is currently visible). If a decision
ever silently vanishes from answers again, check first whether a
`:SaxonRecommendation`-only filter got refactored back to a bare `:Decision`
filter anywhere (`grep -rn "n:Decision" app/`) — the original bug was exactly
one such filter, repeated across `_entity_own_facts`,
`_match_entities_by_name`, `_relationship_path_facts`,
`_relationship_path_full_facts`, `GET /graph/nodes`/`/relationships`/`/summary`,
and `authorization.py`'s equivalents.

---

## 3. Invalidated facts silently dropped when they have no transition partner

**History:** `ContextOrchestrator`'s synthesis step only surfaced an
invalidated fact by pairing it with the newer fact that replaced it (a
"changed from X to Y" line). If the replacement fact wasn't in the *same*
retrieval batch, the old-but-real fact was dropped entirely — not shown
current, not shown as history, just gone — producing a false "No matching
graph context found" even when `metadata.facts` had it the whole time. Hit 3
of the original 8 cross-source questions. Fixed via a shared
`_build_answer_lines` helper that surfaces an orphaned invalidated fact as
"No longer current, but real: ..." — commit `3cf7fb2`. Regression-tested in
`tests/test_dropped_fact_synthesis.py`.

**Regression probe:** Any question whose only relevant facts are superseded
ones with no paired replacement in the batch. This session's baseline
question 5 (*"Is Plant 1 affected?"*) exercises exactly this shape today: the
single relevant fact ("Plant 1 (Toledo) does not share suppliers with Plant 2
on the relay quality issue") is itself marked not-current, and it correctly
rendered as "No longer current, but real: ..." rather than a false
not-found. If this regresses, a negative-control question like this one
starts silently returning "no context found" instead of the historically-true
fact.

---

## 4. Possessive and glued-auxiliary forms breaking entity-name matching

**History:** Found live: `"Ferrotek's"` never resolved to `"Ferrotek
Components"` — the possessive `'s` broke both candidate extraction and
name-matching, triggering the hard "no entity matching that name was found"
short-circuit (reserved for *confident* non-matches) instead of resolving.
Separately, a sentence-initial auxiliary verb could glue onto an adjacent
proper noun ("Has Ferrotek's") into one spurious unmatched candidate. Fixed
in `app/graph/entity_resolution.py`: strips a trailing possessive (ASCII
`'s` and Unicode `'s`) before candidate extraction and again before the
CONTAINS-fallback tier, and strips a leading auxiliary/stopword. Commit
`3cf7fb2`. Confirmed closed 2026-09-03 against the real Ferrotek/CX-17 data
that originally exposed it.

**Regression probe:** *"What's going on with Ferrotek's production line?"*
Confirmed working live in this session's baseline (question 6) — resolved
cleanly to a real Ferrotek Components fact, no false "not found."

---

## 5. Descriptive/role-based entity references never resolve — EXPECTED, not a bug

**History:** Noted explicitly in CLAUDE.md as the one item from the original
8-question set that is **working as designed, not a defect**: a question like
*"the supplier who caused the defect"* or *"the one confirming the fix
shipped"* names no proper entity, so entity resolution (name/ID-pattern
matching only) never attempts a match and the query falls through to
semantic search instead. **Do not "fix" this as a regression** — it's
documented as a known, accepted limitation (a real fix would require
descriptive-reference resolution, out of scope). The only thing worth
watching for is a *worse* failure mode: a descriptive reference incorrectly
CONTAINS-matching onto an unrelated node and hard-short-circuiting the query
(this did happen once, in a different but related bug — see #6 below) rather
than cleanly falling through to search.

**Regression probe:** *"Who is the supplier that caused the defect?"* — a
reasonable answer today comes from semantic search finding "Ferrotek
Components completed corrective action..." (confirmed in this session's
baseline, question 8), not from a resolved named-entity short-circuit. If
this question starts hard-failing with "no entity found" instead of falling
through, something regressed in the fallback logic, not in reference
resolution itself.

---

## 6. Action-log entities silently hijacking a query (related but distinct from #5)

**History:** *"Who approved the expedited fix, and what did it cost?"*
CONTAINS-matched the word "expedited" straight into an unrelated `:Task` node
named "expedited qualification lot," silently anchoring the whole causal walk
on the wrong entity instead of falling through to search (which would have
found the real `DEC-2026-014` approval facts). Fixed with an explicit
exclusion (`NOT (n:Task OR n:Event OR n:Activity OR n:Process OR
n:Transaction OR n:Interaction OR n:Observation)`) applied when restricted to
named-entity resolution, in `app/graph/entity_resolution.py`. Proper-noun and
ID-style candidates (e.g. `QE-2091`) are deliberately unaffected — they
should still match an Event/Issue-typed node when that's genuinely what's
being asked about.

**Regression probe:** *"Who approved the expedited fix, and what did it
cost?"* — must surface the real approval/cost facts (Owen Whitfield's budget
exception, Meridian Circuit Supply's quote), not silently anchor on a
same-named Task/log-entry node.

---

## 7. Causal walk / relationship path crossing a `group_id` (tenant) boundary — proactively tested, not a live incident

**History:** Not found as a live production bug, but flagged and closed as a
latent risk during the same hardening pass: the multi-hop causal `MATCH`
originally constrained only the anchor node's `uuid` and relationship types,
with **no `group_id` check on intermediate/target nodes** — unlike every
other relationship traversal in the codebase
(`app/graph/authorization.py`, `app/api/odata.py`'s `list_facts_odata`).
Since a causal answer also gets written into a permanent, auditable
`:Decision` node, this was worth closing proactively rather than waiting for
a real cross-tenant leak. Regression-tested directly:
`tests/test_causal_chain.py::test_causal_walk_never_crosses_into_another_groups_node`
and `::test_two_entity_path_never_crosses_into_another_groups_node`.

**Regression probe:** Not answerable via the chat UI (requires two tenants'
data in the same Neo4j instance with a crafted cross-group edge) — this is a
test-suite-only regression class. Re-run
`pytest tests/test_causal_chain.py -k cross` before any change to
`causal_chain_for_query`'s Cypher.

---

## 8. Two-entity ("how is X connected to Y") queries silently anchoring on only one entity

**History:** Found live against production data: *"How is Industrial
Automation connected to Diego Alvarez?"* returned "Vantus Robotics operates
in the Industrial Automation industry" — a fact that doesn't mention Diego
Alvarez at all. Root cause: `causal_chain_for_query` had no two-named-entity
case; it silently anchored on whichever of the two candidates happened to
resolve first (candidate ordering, not query semantics) and returned that
entity's own unrelated facts. Fixed by adding a dedicated
shortest-connecting-path branch, mirroring what `search_graphiti_facts`
already had via `_relationship_path_facts`. Commit history: frontend
disclaimer fix landed first (treated the symptom), then the real backend fix.
Regression-tested:
`tests/test_causal_chain.py::test_two_entities_resolve_a_connecting_path_not_just_the_first_ones_own_facts`.

**Regression probe:** *"How is Industrial Automation connected to Diego
Alvarez?"* (or the Solandra-equivalent: *"How is [entity A] connected to
[entity B]?"* for two entities with a real multi-hop path) — the answer must
name **both** entities and trace a real path between them, not one entity's
unrelated own fact.

---

## Still open — do not mistake for something this session should attempt to fix

**Brightpeak Automation account-ownership answer is still wrong on live
data**, reconfirmed in this session's own baseline capture (questions 3 and
4, `eval/baseline_pre_polish.json`): every fact about Brightpeak's account
manager currently returns `is_valid: false`, so both the plain-Ask and
causal-fallback answers correctly say "the facts do not specify who currently
owns the account" — accurate given the underlying data corruption, but not
the right answer (should be Diego Alvarez, per the real Outlook handoff and
the day2 CSV). Root cause per CLAUDE.md: orphaned Graphiti episodes
predating the connector-id tagging fix, with fake sub-second CSV ingestion
timestamps invalidating a real, correctly-dated Outlook fact — and
`purge_connector_data`'s connector-id-scoped matching structurally cannot
reach data with no `connector_id` at all. **A real fix needs new
cleanup tooling** (purge-by-`source_description`/group_id, not just
connector_id) that doesn't exist yet — explicitly logged as future work, not
something to improvise. **Out of scope for this inventory task and for any
"polish" step that isn't itself the dedicated fix for this issue** — flag it
to the owner rather than touching `graph_repository.py`/`connectors.py` to
patch it.
