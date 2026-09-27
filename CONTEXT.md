# Browser Agent

Browser Agent lets one agent observe and operate one logical browser workspace across a task or interactive sequence of tasks.

## Language

**Browser session**:
The browser lifetime owned by one CLI process. Interactive tasks share it; a one-shot invocation owns one session.
_Avoid_: Run, browser instance

**Active tab**:
The sole page target the agent currently observes and acts upon. A newly opened tab replaces it only when adoption is unambiguous.
_Avoid_: Main tab, current page

**Browser action**:
One serialized attempt to change or directly inspect browser state through the active tab.
_Avoid_: Command, operation

**Human intervention**:
A point where automation cannot continue without a person, such as an unresolved verification challenge. Work may resume afterward, but the automation outcome remains failed even when the task ultimately succeeds.
_Avoid_: Manual step, user pause

**Automation outcome**:
Whether a task completed without human intervention. It is distinct from the task outcome.
_Avoid_: Task result, run result

**Harness**:
The caller that starts tasks, supplies configuration and user context, and consumes outcomes from the agent.
_Avoid_: CLI, environment

**User context**:
User-derived or auto-detected regional information supplied by the harness for agent reasoning. It does not imply browser emulation unless the harness explicitly requests an override.
_Avoid_: India defaults, browser identity

**Observation**:
One bounded semantic and visual description of the active tab at a point in time. A newer observation supersedes every target handle from an older one.
_Avoid_: Snapshot, page state

**Target handle**:
An observation-qualified reference to one actionable control. It is valid only for the observation that issued it.
_Avoid_: Element index, locator

**Semantic control**:
An actionable or state-bearing page entity derived from accessibility semantics and enriched with DOM identity.
_Avoid_: Element, node

**Context node**:
Unindexed semantic content that helps interpret controls, such as headings, labels, landmarks, status messages, and validation errors.
_Avoid_: Text element, non-actionable control

**Episode**:
One owned Chrome process using one unique writable browser profile. An episode may span multiple tasks and ends when its browser closes or checkpoint rollover begins.
_Avoid_: Task, run, browser session

**Checkpoint**:
An immutable, restorable browser profile sealed only after its episode's Chrome process exits cleanly.
_Avoid_: Snapshot, state file, backup

**State delta**:
Evidence describing browser-state changes associated with one browser action. It supports inspection and evaluation but cannot restore an episode.
_Avoid_: Checkpoint, replay event

**Artifact**:
Content-addressed evidence captured from an episode, such as semantic state, DOM data, screenshots, storage observations, or network records.
_Avoid_: Log file, checkpoint

### Evaluation

**Eval runner**:
A Harness that runs eval suites under chosen variants and records trials for grading and comparison.
_Avoid_: Eval harness, test harness

**Eval case**:
One task or decision point paired with the graders that judge it.
_Avoid_: Test case, example, sample

**Step eval**:
An eval case that replays a frozen observation, task, and history to judge only the model's next tool call.
_Avoid_: Unit eval, single-turn test

**Task eval**:
An eval case that runs the full agent loop against a fixture site and judges the resulting end state.
_Avoid_: E2E test, scenario

**Fixture site**:
A local, seeded, deterministic web application that task evals run against, whose server-side state graders can inspect.
_Avoid_: Mock site, test page

**Fixture view**:
A named screen of a fixture site that can be loaded directly from a URL and a seed, and recognized in an observation without a model. Eval cases name the views they pass through and the controls each one must offer.
_Avoid_: Page state, fixture page, screen

**Reference solution**:
A human-written path through a task eval's fixture site, derived from the task's intent and replayable without a model, that proves the case solvable and its graders correct. Graders never require the agent to follow it.
_Avoid_: Golden path, expected trajectory

**Simulated user**:
The scripted stand-in for the person during a trial. It answers questions, confirmations, and verification pauses from the eval case's rules; anything unscripted gets a fixed, safe reply and flags the trial.
_Avoid_: Mock user, user simulator

**Eval suite**:
A named, versioned collection of eval cases run together.
_Avoid_: Dataset, benchmark

**Variant**:
One complete, identified bundle of model-facing behaviour under evaluation: a code revision plus its resolved configuration (prompts, tool schemas, observation rendering, model, supervisor, and loop guards).
_Avoid_: Config, arm, flavor

**Trial**:
One execution of one eval case under one variant, producing one trace that graders judge.
_Avoid_: Run, attempt, rollout

**Trace**:
The immutable record of what happened during one trial: what the model saw and did, what the browser and simulated user returned, and the fixture site's end state. Grades refer to a trace; they never alter it.
_Avoid_: Log, transcript, recording

**Grader**:
One check that turns a trial into a binary verdict for a named failure mode, implemented by code, a calibrated LLM judge, or a human label.
_Avoid_: Scorer, metric, evaluator

**Comparison**:
A paired evaluation of a baseline variant against a candidate variant over the same eval suite and trial count, reported per case.
_Avoid_: A/B test, benchmark run

**Observation fidelity check**:
A code check, made without any model call, that a control an eval case needs was captured, kept, described truthfully, shown with its actionability, and rendered unambiguously in what the model saw under a given variant.
_Avoid_: Perception test, observation test

**Failure attribution**:
The label explaining why a failed trial failed — a browser fault (an action landed on a different control than the one chosen), a perception gap, a model decision, or unattributed — derived from observation fidelity checks. It sits beside grades and never changes them.
_Avoid_: Root cause, blame
