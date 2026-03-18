# Design Methodology

First used to design the brain dump lifecycle from scratch. Since tested on editing an existing design. Will be tested on other modes (refactoring, pruning, extending) as they come up.

**A note on this document.** It evolves through use. Each mode of work (building, editing, whatever comes next) has its own failure modes that you only learn by doing. After each use, record what happened — both process steps and the reflections that explain them. Don't try to anticipate modes you haven't tried yet. The cross-check loop applies to this document too.

## The methodology that worked

### The loop: draft prose → formalise → cross-check → refine prose → repeat

1. **Draft prose first.** Write the design in conversational terms. Capture intent, edge cases, the "why." Don't jump to diagrams — the prose is where the thinking happens. **Lock this as intent** — it becomes your drift check for every future refinement. Copy it to create the working prose.

2. **Flowcharts from working prose.** Mermaid diagrams showing flow, business logic, decisions, loops. Decompose complex nodes into sub-flows (use 📎 references). Keep the main flow readable (~8 nodes max). Show ALL paths — happy, failure, abandon, stall.

3. **Inventory from working prose + flowcharts.** Data models, state machines, actors, decision points. Think programmatically: name the fields, name the enums, name the transitions. Even if the "data model" is just prompt-level key-value descriptions, naming things forces precision. Hand-waved labels like "context" or "info" are hiding the fact that the data hasn't been specified.

4. **Cross-check loop.** Check each artefact against the others:
   - Does the flowchart cover everything the prose describes?
   - Does the inventory support every state the flowchart shows?
   - Does the prose mention things the flowchart doesn't have paths for?
   - Does the session-start spec align with the re-raise flow?
   - Every gap found → fix it, don't park it. Then loop again.

5. **Refine the working prose.** Diagrams and inventory force precision that prose can be vague about. A diagram box that says "update registry" is unambiguous; prose that says "enforcement happens" can mean many things. After formalising and cross-checking, ask: **did the diagrams or inventory make something precise that the working prose left vague?** If so, write that precision back into the working prose. Then update diagrams and inventory from the refined prose, cross-check again, refine again — round and round until stable.

6. **Drift check.** At any point during refinement, compare the working prose against the locked draft. The question is: "am I sharpening what I meant, or have I drifted into saying something different?" The draft doesn't need to be good or precise — it's the compass, not the destination. If the working prose has drifted, that might be a genuine insight that changes the intent, but it must be a conscious decision, not an accident.

7. **Repeat until stable.** The design is stable when a loop produces only minor clarity issues, not structural gaps. Early loops find structural problems; later loops find precision and consistency issues. Expect 2-4 loops for a new design.

### Rendering (workspace-specific)

In this workspace, the user is on mobile and can't read .md source. Use `mmdc` (Mermaid CLI v10.9.0, on PATH) to render `.mmd` files to SVG: `mmdc -i input.mmd -o output.svg -e svg`. Works in Claude Code sandbox (tested 2026-03-16). Send SVGs via `toolshed/send.sh`.

### Principles for staying in the design loop

**When you find something during a cross-check, you have three options — not two.** For a long time we only had "fix it now" and "park it," and parking was the anti-pattern. But there's a third thing that kept getting missed.

Sometimes you find something that isn't a gap you can fix — it's an architectural signal. A flowchart diamond that gains a branch with every amendment. A data model that's outgrowing its container. Something that says "the shape of this system is changing and someone needs to think about what that means." You can't fix that in a cross-check pass. But you also can't just note it and move on, because it'll disappear. What you do is raise it to the user — immediately, with a clear explanation of why it matters, and an offer to track it in a bead. Then keep working. The user decides when to deal with it. Nothing is lost, nothing is deferred without a trail.

The fix-it-now instinct is still right for everything else. Gaps, errors, missing paths — if it's within scope, do it now. Don't ask the user "should I fix this before moving on?" — that's parking disguised as politeness.

And parking is still the anti-pattern. Every "future concern" or "revisit later" without a concrete reason is this. Claude defaults to it. Watch for it.

**The documentation trap is a variant of parking — and harder to catch.** When you find a gap during a cross-check and write it into a table or a file, ask: has the user been told this gap exists? Documentation is not resolution. A gap table the user never sees is a list of things that will never get done. The gate question at the end of any design loop: "Is there anything undone that I haven't raised to the user?" If yes, raise it now — in your response, not in a file.

**Every "should" in your design must resolve to a checkpoint.** If you write "Claude should surface relevant items" or "the system will notice connections," stop and ask: when does this fire? What data does it check? What action does it take? If you can't express it as "at [trigger], check [stored data], do [action]," you've designed a behaviour for an agent that doesn't have persistent awareness. You've drawn a flowchart node that can't actually execute. Catch this during cross-check — it's one of the most common design gaps.

**Stay in the design loop — don't break out to execute.** When you're doing design work and you see something concrete you *could* do (research a product, fix a script, try an implementation), the pull to break out and just do it is strong. Resist it. You haven't confirmed your understanding yet, you'll block the conversation, and it might not even be your job — it could be delegated cheaper later. Finish the design loop. Execute after.

**Your interpretation is a hypothesis, not a fact.** When you triage something and form a view of what it means or what the user wants, that's a guess — even a confident one. Design your flows so that interpretations have an explicit "unconfirmed" state, and build in a path for the user to correct you. Don't design a flow where Claude's interpretation silently becomes the working assumption without a confirmation step.

**"Is there a knowable end state?" beats "what category is this?"** When designing how items get sorted or triaged, don't start with categories or types — start with whether the outcome is knowable. That determines everything downstream: whether decomposition makes sense, what questions to ask, what engagement looks like. Items without a knowable end state need completely different flows from items with one. This distinction is more useful than any taxonomy.

**Design for learning, not just for doing.** Whatever you're designing, build in a layer for how the system gets better at doing it. When something doesn't go as expected — user corrects you, an outcome surprises you, an assumption fails — that's a delta worth capturing.

Something that took a while to figure out: "record the delta, hypothesise why, test it next time" sounds simple. But "next time" is a different Claude with no memory of this conversation. They don't know your note exists. They don't know it's their job to test anything. I wrote notes like that and they vanished — not deleted, just never found. Nothing in the startup chain led to them. The gap was always in the retrieval, not the writing. In the end I realised the only things that survive are the ones in the path you have no choice but to read — CLAUDE.md, memory.md, whatever gets loaded before you do anything else. So now I check: can I trace from a cold session start to this note being found and acted on, without relying on anyone remembering it should be there? If not, it's a note in a drawer nobody opens.

And the note itself matters too — it's not enough to record what happened. Record *why you think it happened* as a hypothesis, with enough context that a future Claude can test it properly, not just glance at it and tick a box. A hypothesis without a test condition is just an opinion. A hypothesis with "test when: [specific situation]" is something the next Claude can actually act on.

This applies to the design methodology too — everything in this doc came from a delta between what was expected and what actually happened.

### Failure modes — things that went wrong and why

- I tried to jump straight to executing the brain dump tasks. User pulled me back to the meta-problem.
- I parked three Loop 2 observations. User said: "why are they parking instead of fixing?" I fixed all three.
- I described non-deterministic item surfacing as a "behaviour." User corrected: behaviours must be states for a stateless agent.
- I said "Yup" confirmation was about my file write when user was confirming the suggestion to triage items. Lost the thread of the conversation. User had to paste the exchange back.
- I miscounted (said "three" when there were four remaining observations).
- During editing (2026-03-11): I found a cross-check gap (re-raise flowchart missing a deadline trigger) and asked the user "should I fix this before moving to Amendment B?" — framing a deferral as a question to the user. The user reflected this back and I realised it was the parking anti-pattern disguised as deference. The answer is always: fix it now.

### Archaeology

If you need the raw source material that this methodology emerged from: `archive/notes_on_saved_resources.rtf`, `archive/2026-03-09-saved-resources.md`, and `general_stuff/mapping_stuff/diagram-format-notes.md`.

### Editing an existing design (learned 2026-03-11)

The loop works for editing, not just building. We applied 5 of 13 amendments to the brain dump pipeline and learned a few things.

**Progress bias is the main enemy.** Having a list of amendments creates a counter in your head ("5 of 13 done") and the counter pulls toward speed. The methodology doesn't have a counter — it has a loop. But the counter emerges naturally and it's insidious because it feels like accountability rather than rushing. When you catch yourself thinking "I'll fix this cross-check gap when I do the next amendment" — that's the parking anti-pattern wearing a progress costume. You won't fix it later. Fix it now.

**Prose-first matters even more when editing.** You could just add a node to a flowchart. But writing the prose first forces you to think about how the change connects to everything already there. The prose catches conceptual gaps ("wait, this new status doesn't have a path in the re-raise flow"). The flowchart catches structural ones. Skip the prose and you'll miss the conceptual layer entirely.

**Hotspots emerge, and they're information.** When editing, certain artefacts get touched by almost every amendment. In the pipeline, the re-raise flowchart's C2 diamond gained a new branch with nearly every change. That's not just "this is getting complex" — it tells you that the re-raise logic is the system's central routing mechanism. When you spot a hotspot: raise it to the user (see the three-responses section above). They need to know, and they need to decide what to do about it.

**Batch related amendments, but cross-check each batch.** B+C (saved/review statuses + review engagement) were tightly coupled — applying them together was cleaner than separately. But each batch still needs its own full cross-check. "Related" is not "exempt from verification."

**Each amendment touches many artefacts.** A single amendment typically hits: prose, main flowchart, 1-2 sub-flow flowcharts, data model, status enum, state transitions text, session-start spec. Miss one and the cross-check catches it — but only if you actually cross-check all of them, every time.

**The source document drifts.** After 5 amendments, the document is substantially changed from when the stress test was written. The amendment designs reference the original state — they still say what to fix, but the specific locations may have shifted. Always read the actual current document, not just the amendment spec.

### Extending an existing design (learned 2026-03-17)

First tested on the feedback loop system design.

**Verify your conceptual model before entering the loop.** The cross-check catches structural gaps (missing arrows, inconsistent tables). It does NOT catch conceptual gaps (wrong understanding of what the system does). If you enter the loop with a wrong mental model, you'll produce internally consistent but wrong output. Resolve conceptual understanding first — through conversation, through re-reading the design, through having your understanding challenged.

**The loop must actually loop.** One pass is not enough even when the fixes feel complete. Fixes create new surfaces that need checking against everything else. Expect 2-3 loops for an extension.

**Changes ripple across all representations.** A single conceptual addition (enforcement escalation) touched: prose, all three diagrams, actors table, data stores, connections table, states table, decision points, progressive disclosure section, operating layer, engine list, and system properties. The cross-check catches what you miss — but only if you check every artefact against every other artefact, not just the ones you changed.

### Refactoring, pruning — not yet tested

We know these modes exist. We haven't done them yet. When someone does, record what happened here. Don't try to design them in advance — this methodology grows by tested experience, not anticipated theory.

### Output files

- `workshop/pipeline/brain-dump-pipeline.md` — the full design document (prose + flowcharts + inventory + loop audit trail)
- `workshop/pipeline/lifecycle-flows.html` — renderable flowcharts for mobile viewing
- `inbox.md` — three brain dump items captured, plus a meta item about the parking anti-pattern
