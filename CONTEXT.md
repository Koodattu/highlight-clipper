# Highlight Clipping

This context describes how the tool turns one creator's long source recording into a small set of human-reviewed clips tailored to that creator's editorial preferences.

## Language

### Inputs and Preferences

**Source Import**:
The validated stream-selection, copy, Media Timeline, and aligned-derivative operation that creates an immutable Source Recording owned by the clipping workflow.
_Avoid_: Upload, register path, move file

**Source Recording**:
The immutable local video file owned by the clipping workflow from which Clip Proposals and Accepted Clips are derived.
_Avoid_: VOD URL, downloaded asset, working copy

**Media Timeline**:
The immutable mapping from selected source streams to Source Time, including its video-defined origin, exclusive end, and audio alignment.
_Avoid_: Probe metadata, proxy timestamps, mutable manifest

**Source Time**:
Decimal seconds from the first playable frame of the selected video stream, where that frame is 0.0. It is the sole time coordinate used for all editorial concepts.
_Avoid_: Proxy time, model time, wall-clock time

**Creator Profile**:
The single creator's Finnish-and-English language use, desired content categories, exclusions, and editorial preferences used to judge Clip Proposals. Switching languages within the same utterance is normal creator behavior.
_Avoid_: User account, global defaults, audience profile

### Local Runtime

**Work Directory**:
The repository-local, Git-ignored `workdir/` that contains every downloaded model and runtime, private Source Recording, database, generated artifact, cache, log, temporary file, local backup, and export.
_Avoid_: Repository, project files, global cache

**Runtime Bundle**:
A versioned set of `llama-server.exe` and its matching backend and CUDA runtime libraries from one pinned llama.cpp build.
_Avoid_: Latest binary, installed llama.cpp, executable path

**Model Profile**:
A reproducible evaluator identity consisting of a model repository and revision, exact GGUF hashes, runtime build, context and MTP settings, reasoning and sampling policy, prompt/schema version, and validation policy.
_Avoid_: Model name, friendly tag, server command

**GPU Lease**:
Exclusive permission for one disposable worker process tree to own CUDA resources until that tree has exited and GPU memory release diagnostics have been recorded.
_Avoid_: GPU lock file, loaded model, worker slot

### Editorial Workflow

**Analysis Run**:
An immutable derivation of Observations, Candidate Moments, Clip Proposals, and a Queue Snapshot from one already-imported Source Recording under one Creator Profile revision and one reproducible configuration. Review and export happen after the Analysis Run finishes.
_Avoid_: Job, processing session, current analysis

**Observation**:
Timestamped evidence about a Source Recording derived from its media or associated creator activity.
_Avoid_: Signal, highlight, judgment

**Evidence Item**:
An immutable, source-linked unit of transcript or Observation content that can be cited when retrieving or evaluating a moment.
_Avoid_: Free-text evidence, model rationale, timestamp string

**Candidate Moment**:
A high-recall point or short span identified from one or more Observations. It anchors further evaluation but does not yet have editorial clip boundaries.
_Avoid_: Highlight, Clip Proposal, final clip

**Candidate Cluster**:
A group of nearby or semantically similar Candidate Moments evaluated together while preserving every contributing moment and Evidence Item.
_Avoid_: Merged candidate, deduplicated highlight

**Highlight**:
A moment the creator judges worth publishing or retaining, whether or not the system found it.
_Avoid_: Candidate Moment, Clip Proposal, model selection

**Reference Moment**:
A manually annotated potential Highlight used to evaluate retrieval and boundary quality, marked as definite or possible with a category, ideal interval, and Event anchor.
_Avoid_: Reference Highlight, gold label, test candidate

**Context Envelope**:
The surrounding interval examined to decide whether a Candidate Moment can become a coherent Clip Proposal.
_Avoid_: Clip, fixed window

**Clip Proposal**:
A machine-suggested immutable interval from a Source Recording, including proposed boundaries and the evidence for reviewing it. It has not yet been approved for export.
_Avoid_: Highlight, final clip

**Review Queue**:
A ranked set of Clip Proposals awaiting human editorial judgment.
_Avoid_: Highlight list, output gallery

**Queue Snapshot**:
A fixed ranked version of a Review Queue whose proposal identities and order do not change during review.
_Avoid_: Live query, current ranking, mutable queue

**Editorial Decision**:
The editor's recorded accept, maybe, reject, or withdrawal judgment on a Clip Proposal, including any correction to its boundaries and the history of later revisions.
_Avoid_: Model label, prediction

**Rejection Reason**:
The editor's primary explanation for rejecting a Clip Proposal: no payoff, too much required context, routine content, repetition, poor media quality, poor Creator Fit, publication Risk, or unusable proposed boundaries. A correctable interval can also be captured as a Boundary Edit.
_Avoid_: Feedback, note, model rationale

**Boundary Edit**:
The editor's optional correction to a Clip Proposal's proposed start or end, whether the proposal is accepted, deferred, or rejected. It records how much Setup and Payoff the creator actually wanted.
_Avoid_: Trim, timestamp fix

**Boundary Anchor**:
A machine-selectable Source Time point derived from an envelope edge, transcript boundary, pause, or later visual Observation.
_Avoid_: Invented timestamp, free-form offset

**Accepted Clip**:
The approved editorial interval recorded when the current Editorial Decision revision is accept. It may have zero or more rendered Exports.
_Avoid_: Rendered file, auto-generated highlight

**Export**:
A rendered media artifact produced from one exact Accepted Clip and Source Recording identity/hash.
_Avoid_: Accepted Clip, final decision

**Personalized Ranker**:
A model trained from the creator's Editorial Decisions and Boundary Edits to order Clip Proposals within each Source Recording.
_Avoid_: Semantic evaluator, excitement score

### Proposal Structure

**Setup**:
The minimum preceding context needed to understand the central moment.
_Avoid_: Background, pre-roll

**Hook**:
The earliest line or action within a Clip Proposal that can earn the viewer's attention. It may coincide with the Event.
_Avoid_: Start time, teaser

**Event**:
The central occurrence, claim, or exchange that makes a Clip Proposal worth considering.
_Avoid_: Candidate Moment, action

**Payoff**:
The resolution, reaction, revelation, or takeaway that rewards attention to the Clip Proposal.
_Avoid_: Punchline, ending

**Exit**:
The clean stopping point after the Payoff and before dead air, repetition, or unrelated material.
_Avoid_: End time, outro

### Proposal Judgments

**Salience**:
The degree to which a Clip Proposal contains something notable enough to deserve editorial attention.
_Avoid_: Excitement, virality

**Standalone Coherence**:
The degree to which a viewer can understand a Clip Proposal without seeing the rest of the Source Recording.
_Avoid_: Context score, clarity

**Hook Strength**:
The degree to which the Hook can earn attention early without misrepresenting the Clip Proposal.
_Avoid_: Hook, opening score

**Payoff Strength**:
The degree to which the Payoff resolves or rewards the Setup and Event.
_Avoid_: Payoff, ending score

**Creator Fit**:
The degree to which a Clip Proposal matches the publishing preferences in the Creator Profile.
_Avoid_: Generic quality, audience fit

**Short-form Suitability**:
The degree to which a Clip Proposal can preserve its Setup and Payoff within a useful short-form duration.
_Avoid_: Virality, brevity

**Context Sufficiency**:
The degree to which the available Context Envelope contains enough Setup and resolution to judge a coherent Clip Proposal honestly.
_Avoid_: Context missing, transcript completeness

**Risk**:
A potential reason not to publish a Clip Proposal, including privacy, hateful language, copyrighted material, or exposed personal information. Risk can motivate a human rejection but is not itself a machine quality judgment.
_Avoid_: Low quality, automatic rejection

### Proposal Categories

**Reaction**:
A notable gameplay event or immediate creator response, such as a clutch, scare, or failure.
_Avoid_: Hype, gameplay highlight

**Comedy**:
A moment whose humor has an understandable setup and payoff, including banter and misunderstandings.
_Avoid_: Funny moment, joke clip

**Story**:
A self-contained anecdote with meaningful progression toward a revelation or payoff.
_Avoid_: Anecdote, ramble

**Opinion**:
A coherent stance or argument with a clear, quotable point.
_Avoid_: Rant, hot take

**Explanation**:
A self-contained explanation or strategic discussion with a useful takeaway.
_Avoid_: Educational clip, tutorial
