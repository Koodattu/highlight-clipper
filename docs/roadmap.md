# Product roadmap

**Status:** Directional; features are promoted by evidence rather than date

The product remains a personalized multimodal retrieval and ranking system. Milestone 1 deliberately establishes a complete text/audio-first editorial loop because reliable creator-specific labels must exist before learned personalization, and because every additional modality should prove that it improves the creator's workflow.

This sequence narrows the first release without narrowing the architecture.

## Principles

- Ship complete vertical workflows before broad collections of disconnected detectors.
- Keep Source Time, Evidence Item, Candidate Moment, Candidate Cluster, Context Envelope, Clip Proposal, Queue Snapshot, Editorial Decision, Boundary Edit, and Export semantics stable across later stages.
- Prefer adding new signals through adapters and independent generators. A measured stage may be replaced when held-out evidence justifies it and a migration-compatible contract preserves creator-owned history.
- Promote a component only when a whole-recording held-out comparison improves recall, boundary quality, acceptance rate, diversity, or review time enough to justify its runtime and maintenance cost.
- Preserve raw observations and creator-owned decisions so future models can be evaluated without relabeling old recordings.
- Keep publication under human control until separate evidence supports broader automation.

## Milestone 1: trustworthy local review loop

- Immutable local Source Recording import and Source Time alignment.
- Review proxy and analysis audio.
- Finnish/English code-switching ASR.
- Lightweight audio and transcript observations.
- Independent high-recall candidate generators.
- Structured local semantic evaluation and variable boundaries.
- Transparent ordering, diversity, immutable Queue Snapshots, and human review.
- Source-aspect export from the original.
- Append-only learning data, portable annotation backup, and reproducible evaluation.

Milestone 1 is text/audio-first. It does not claim to be multimodal or learned-personalized; it creates the reliable system and data those capabilities require.

## Signal expansion

Add individually and retain only after ablation:

- local Twitch chat/event sidecars with measured Source Time offset;
- pitch, eGeMAPS, laughter, and audio-event observations;
- historical Twitch clips, manual markers, and owned YouTube retention data;
- speaker-aware processing or separate microphone/game/guest tracks when available;
- source separation only for recordings where it improves downstream results.

Community Interaction becomes a first-class proposal category when chat/interaction evidence is available and improves held-out coverage.

These become new Observations and independent Candidate Moment generators. They do not bypass the existing merge, evaluation, review, or lineage contracts.

## Selective visual understanding

Add inexpensive full-recording observations first:

- sparse frame and scene sampling;
- black/static/loading/BRB detection;
- motion and facecam movement;
- OCR on known layout regions;
- game-specific adapters for recurring titles.

Visually driven events become a first-class category only when the system has visual Evidence Items rather than inferring them from transcript/audio.

Use a VLM only for visually driven, uncertain, or high-ranked Context Envelopes. Compare transcript/numeric evidence, storyboard evidence, and candidate-video evidence separately. A VLM remains one evidence producer or reranker, not the authority for editorial quality or safety.

## Learned personalization

Train the first Personalized Ranker only after at least 200 Editorial Decisions across eight Source Recordings. Begin with logistic regression as a sanity baseline and a small tree-based ranker for nonlinear interactions.

Training and evaluation group by whole Source Recording. The learned ranker replaces the fixed baseline only when it improves a frozen held-out evaluation without unacceptable category collapse, latency, or instability. Boundary Edits inform a separate boundary preference model only when enough examples exist.

Later signals may include historical publication decisions and downstream performance, but platform views and retention remain biased observations rather than ground-truth quality labels.

## Packaging and publication workflow

After retrieval and boundaries are reliable:

- short, medium, and extended variants from one Highlight;
- vertical crop and layout profiles;
- word-timed subtitles and speaker-aware styling;
- draft titles and descriptions;
- final audio, privacy, copyright, and content review;
- optional upload integrations with explicit approval.

Rendering remains downstream of editorial acceptance so packaging experiments do not contaminate detection labels.

## Future-stream instrumentation

Future capture can improve evidence quality through:

- separate OBS audio tracks;
- scene and category changes;
- EventSub chat and notifications;
- Stream Deck, moderator, or manual moment markers;
- local live audio summaries;
- URL acquisition through replaceable import adapters.

Markers are high-quality creator input, not a failure of automation.

## Stable extension seams

Later work extends these contracts:

| Extension | Responsibility |
|---|---|
| Source importer | Produces an immutable local Source Recording and time manifest |
| Observation producer | Emits versioned evidence in Source Time |
| Candidate generator | Emits high-recall Candidate Moments without globally comparable confidence |
| Evaluator adapter | Turns Context Envelopes into validated proposal outcomes |
| Ranker | Orders immutable proposals within one Source Recording |
| Diversity selector | Creates a stable, versioned Queue Snapshot |
| Renderer | Produces validated variants from a confirmed Editorial Decision |

No roadmap phase requires changing creator-owned history in place. New analysis models and signals create comparable Analysis Runs; new renderer settings create Export generations without rerunning analysis.
