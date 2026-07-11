const token = document.querySelector('meta[name="highlight-clipper-token"]').content;
const state = { data: null, view: "review", queue: null, proposalIndex: 0, reviewFilter: "all", stopAt: null, referenceSource: null, referenceGeneration: 0, reviewSession: crypto.randomUUID(), reviewSequence: 0, lastReviewInteraction: Date.now(), lastReviewTick: Date.now(), reviewFlushInFlight: false, pendingReviewActivity: null, waveformGeneration: 0, waveformBins: [] };
const $ = (selector) => document.querySelector(selector);
const categoryKeys = ["reaction", "comedy", "story", "opinion", "explanation"];
const defaultDurations = { reaction: [15, 60], comedy: [20, 90], story: [45, 180], opinion: [30, 180], explanation: [60, 240] };

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if ((options.method || "GET") !== "GET") headers["X-Highlight-Clipper-Token"] = token;
  const response = await fetch(path, { ...options, headers });
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.style.background = isError ? "#7f2c27" : "#1f2926";
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 2800);
}

function seconds(us) { return (Number(us) / 1_000_000).toFixed(3); }
function editableSeconds(us) { return (Number(us) / 1_000_000).toFixed(6); }
function freshKey() { return crypto.randomUUID(); }
function markReviewInteraction() { state.lastReviewInteraction = Date.now(); }
function reviewProposals() {
  const proposals = state.queue?.proposals || [];
  return state.reviewFilter === "maybe"
    ? proposals.filter((proposal) => proposal.current_decision?.decision === "maybe")
    : proposals;
}

function referenceRequestIsCurrent(generation, sourceId) {
  return generation === state.referenceGeneration
    && state.view === "references"
    && state.referenceSource?.id === sourceId;
}

function boundariesMatchCurrentDecision(proposal) {
  const savedStart = Number(proposal.current_decision?.boundary_start_us ?? proposal.start_us);
  const savedEnd = Number(proposal.current_decision?.boundary_end_us ?? proposal.end_us);
  const visibleStart = Math.round(Number($("#boundary-start").value) * 1_000_000);
  const visibleEnd = Math.round(Number($("#boundary-end").value) * 1_000_000);
  return Number.isFinite(visibleStart) && Number.isFinite(visibleEnd)
    && visibleStart === savedStart && visibleEnd === savedEnd;
}

function updateBoundaryActionState(proposal) {
  const current = proposal.current_decision;
  const dirty = !boundariesMatchCurrentDecision(proposal);
  const staleEvidence = Boolean(current?.outside_evaluated_context);
  $("#decision-state").textContent = current
    ? `Current decision: ${current.decision} · revision ${current.revision_number}${staleEvidence ? " · edited interval extends beyond evaluated evidence" : ""}${dirty ? " · save the decision to apply these boundaries" : ""}`
    : dirty ? "Unsaved boundary changes · save a decision to apply them" : "Awaiting your decision";
  $("#reanalyze-boundary").classList.toggle("is-hidden", !staleEvidence);
  $("#reanalyze-boundary").disabled = !staleEvidence || dirty;
  $("#export").disabled = current?.decision !== "accept" || dirty;
}

function updateEmptyState() {
  const empty = $("#empty-state");
  const eyebrow = empty.querySelector(".eyebrow");
  const title = empty.querySelector("h2");
  const detail = empty.querySelector("p:not(.eyebrow)");
  const command = empty.querySelector("code");
  if (state.view === "review" && state.queue && state.reviewFilter === "maybe") {
    eyebrow.textContent = "No Maybe decisions";
    title.textContent = "Nothing is waiting for a second look.";
    detail.textContent = "Mark a proposal Maybe and it will appear here without changing the immutable queue.";
    command.classList.add("is-hidden");
    return;
  }
  if (state.view === "review" && state.queue) {
    eyebrow.textContent = "Queue is empty";
    title.textContent = "This analysis did not produce reviewable proposals.";
    detail.textContent = "Request more candidates or run another model profile while keeping this queue as evidence.";
    command.classList.add("is-hidden");
    return;
  }
  eyebrow.textContent = "No queue selected";
  title.textContent = "Import a recording, run analysis, then review the evidence.";
  detail.textContent = "Large files enter through the CLI. This browser stays focused on editorial work.";
  command.textContent = "uv run highlight-clipper import C:\\path\\to\\recording.mp4";
  command.classList.remove("is-hidden");
}

async function refresh() {
  state.data = await api("/api/bootstrap");
  const active = state.data.runs.find((run) => run.state === "running");
  const failed = state.data.tasks.find((task) => task.state === "failed");
  $("#system-status").textContent = active ? "Analysis running" : failed ? "Action needed" : "Local · Ready";
  renderSidebar();
  if (state.data.tasks.some((task) => ["pending", "running"].includes(task.state))) {
    window.setTimeout(refresh, 1200);
  }
}

function renderSidebar() {
  const root = $("#sidebar-content");
  root.replaceChildren();
  if (state.view === "review") renderQueues(root);
  if (state.view === "sources") renderSources(root);
  if (state.view === "profile") renderProfile(root);
  if (state.view === "references") renderReferencePicker(root);
  syncWorkspace();
}

function syncWorkspace() {
  const referenceMode = state.view === "references";
  const reviewMode = state.view === "review";
  const referencePlayer = $("#reference-player");
  const reviewPlayer = $("#player");
  updateEmptyState();
  $("#reference-card").classList.toggle("is-hidden", !referenceMode);
  if (!referenceMode) referencePlayer.pause();
  if (!reviewMode) {
    reviewPlayer.pause();
    state.stopAt = null;
    state.waveformGeneration += 1;
    $("#review-card").classList.add("is-hidden");
    $("#empty-state").classList.toggle("is-hidden", referenceMode);
    return;
  }
  referencePlayer.pause();
  const hasProposal = Boolean(reviewProposals().length);
  $("#review-card").classList.toggle("is-hidden", !hasProposal);
  $("#empty-state").classList.toggle("is-hidden", hasProposal);
}

function heading(title, detail) {
  const box = document.createElement("div");
  box.className = "side-heading";
  const titleElement = document.createElement("h3");
  titleElement.textContent = title;
  const count = document.createElement("small");
  count.className = "muted counter";
  count.textContent = detail;
  box.append(titleElement, count);
  return box;
}

function renderQueues(root) {
  root.append(heading("Review queues", `${state.data.queues.length}`));
  const filters = document.createElement("div");
  filters.className = "tabs";
  [["all", "All proposals"], ["maybe", "Maybe"]].forEach(([value, label]) => {
    const button = document.createElement("button");
    button.className = `tab ${state.reviewFilter === value ? "is-active" : ""}`;
    button.textContent = label;
    button.addEventListener("click", () => {
      state.reviewFilter = value;
      state.proposalIndex = 0;
      renderSidebar();
      renderProposal();
    });
    filters.append(button);
  });
  root.append(filters);
  const list = document.createElement("div");
  list.className = "side-list";
  state.data.queues.forEach((queue) => {
    const button = document.createElement("button");
    button.className = `side-item ${state.queue?.snapshot?.id === queue.id ? "is-active" : ""}`;
    const label = document.createElement("strong");
    label.textContent = `Queue · ${queue.proposal_count} proposals`;
    const detail = document.createElement("small");
    detail.textContent = new Date(queue.created_at).toLocaleString();
    button.append(label, detail);
    button.addEventListener("click", () => selectQueue(queue.id));
    list.append(button);
    const queueCap = Number(queue.configuration?.max_queue_size || 30);
    if ((queue.configuration?.budget_tier || "default") === "default" && Number(queue.proposal_count) < queueCap) {
      const more = document.createElement("button");
      more.className = "button secondary";
      more.textContent = "Request more";
      more.addEventListener("click", async () => {
        if (!window.confirm("Start an expanded analysis and preserve this queue as its ranked prefix?")) return;
        try {
          await api(`/api/queues/${queue.id}/more`, { method: "POST", body: "{}" });
          showToast("Expanded analysis started");
          await refresh();
        } catch (error) { showToast(error.message, true); }
      });
      list.append(more);
    }
  });
  if (!state.data.queues.length) {
    const message = document.createElement("p");
    message.className = "muted";
    message.textContent = "Completed analyses will appear here.";
    list.append(message);
  }
  root.append(list);
}

function renderSources(root) {
  root.append(heading("Source recordings", `${state.data.sources.length}`));
  const list = document.createElement("div");
  list.className = "side-list";
  state.data.sources.forEach((source) => {
    const card = document.createElement("div");
    card.className = "side-item";
    const name = document.createElement("strong");
    name.textContent = source.original_name;
    const detail = document.createElement("small");
    detail.textContent = `${seconds(source.source_end_us)} sec · ${source.sha256.slice(0, 10)}…`;
    const run = document.createElement("button");
    run.className = "button primary";
    run.textContent = "Run local analysis";
    run.addEventListener("click", () => startAnalysis(source.id, "real"));
    const fake = document.createElement("button");
    fake.className = "button ghost";
    fake.textContent = "Diagnostic fake run";
    fake.addEventListener("click", () => startAnalysis(source.id, "fake"));
    card.append(name, detail, run, fake);
    list.append(card);
  });
  root.append(list);
  renderAnalysisTasks(root);
}

function renderAnalysisTasks(root) {
  const tasks = state.data.tasks || [];
  if (!tasks.length && !state.data.runs.some((run) => ["failed", "cancelled"].includes(run.state))) return;
  root.append(heading("Analysis activity", `${tasks.length}`));
  const list = document.createElement("div");
  list.className = "side-list";
  tasks.slice().reverse().forEach((task) => {
    const card = document.createElement("div");
    card.className = "side-item";
    const label = document.createElement("strong");
    label.textContent = `${task.mode === "fake" ? "Diagnostic" : "Local"} analysis · ${task.state}`;
    const detail = document.createElement("small");
    const progress = Math.round(100 * Number(task.overall_progress || 0));
    detail.textContent = task.stage ? `${task.stage.stage_name} · ${progress}% · attempt ${task.stage.attempt_number}` : "Starting";
    card.append(label, detail);
    if (task.error) {
      const error = document.createElement("small");
      error.textContent = task.error;
      error.className = "error-text";
      card.append(error);
    }
    if (["pending", "running"].includes(task.state)) {
      const cancel = document.createElement("button");
      cancel.className = "button ghost";
      cancel.textContent = task.cancel_requested ? "Cancellation requested" : "Cancel";
      cancel.disabled = Boolean(task.cancel_requested);
      cancel.addEventListener("click", async () => {
        try {
          await api(`/api/analysis-tasks/${task.id}/cancel`, { method: "POST", body: "{}" });
          showToast("Cancellation requested");
          await refresh();
        } catch (error) { showToast(error.message, true); }
      });
      card.append(cancel);
    }
    list.append(card);
  });
  const activeRunIds = new Set(tasks.map((task) => task.analysis_run_id));
  state.data.runs.filter((run) => ["failed", "cancelled"].includes(run.state) && !activeRunIds.has(run.id)).forEach((run) => {
    const card = document.createElement("div");
    card.className = "side-item";
    const label = document.createElement("strong");
    label.textContent = `Analysis · ${run.state}`;
    const detail = document.createElement("small");
    detail.textContent = run.latest_stage?.error_summary || new Date(run.created_at).toLocaleString();
    card.append(label, detail);
    if (run.state === "cancelled" || run.latest_stage?.retryable) {
      const retry = document.createElement("button");
      retry.className = "button secondary";
      retry.textContent = "Retry completed work";
      retry.addEventListener("click", async () => {
        try {
          await api(`/api/analysis-runs/${run.id}/retry`, { method: "POST", body: "{}" });
          showToast("Analysis retry started");
          await refresh();
        } catch (error) { showToast(error.message, true); }
      });
      card.append(retry);
    }
    list.append(card);
  });
  root.append(list);
}

async function startAnalysis(sourceId, mode) {
  try {
    await api(`/api/sources/${sourceId}/analyses`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    showToast(mode === "real" ? "Local analysis started" : "Diagnostic analysis started");
    await refresh();
  } catch (error) { showToast(error.message, true); }
}

function renderProfile(root) {
  const profile = state.data.profiles[0];
  root.append(heading("Creator profile", profile ? `Revision ${profile.revision_number}` : "New"));
  const form = document.createElement("form");
  form.className = "form-stack";
  form.innerHTML = `
    <label>Languages<select name="languages" multiple><option value="fi">Finnish</option><option value="en">English</option></select></label>
    <label>Desired content<textarea name="desired" rows="4"></textarea></label>
    <label>Avoided content<textarea name="avoided" rows="4"></textarea></label>
    <fieldset><legend>Category priorities · 0–4</legend><div class="profile-grid">${categoryKeys.map((key) => `<label>${key}<input name="priority_${key}" type="number" min="0" max="4" step="1" required></label>`).join("")}</div></fieldset>
    <fieldset><legend>Preferred durations · seconds</legend><div class="duration-list">${categoryKeys.map((key) => `<div><span>${key}</span><label>Min<input name="duration_${key}_min" type="number" min="1" max="239" step="1" required></label><label>Max<input name="duration_${key}_max" type="number" min="2" max="240" step="1" required></label></div>`).join("")}</div></fieldset>
    <button class="button primary" type="submit">Save new revision</button>`;
  const languageSelect = form.elements.languages;
  Array.from(languageSelect.options).forEach((option) => { option.selected = (profile?.languages || ["fi", "en"]).includes(option.value); });
  form.elements.desired.value = profile?.desired_content || "";
  form.elements.avoided.value = profile?.avoided_content || "";
  categoryKeys.forEach((key) => {
    const duration = profile?.preferred_durations?.[key] || defaultDurations[key];
    form.elements[`priority_${key}`].value = profile?.category_priorities?.[key] ?? 1;
    form.elements[`duration_${key}_min`].value = duration[0];
    form.elements[`duration_${key}_max`].value = duration[1];
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const languages = Array.from(languageSelect.selectedOptions).map((option) => option.value);
    const priorities = Object.fromEntries(categoryKeys.map((key) => [key, Number(form.elements[`priority_${key}`].value)]));
    const durations = Object.fromEntries(categoryKeys.map((key) => [key, [Number(form.elements[`duration_${key}_min`].value), Number(form.elements[`duration_${key}_max`].value)]]));
    try {
      await api("/api/profiles", { method: "POST", body: JSON.stringify({ languages, category_priorities: priorities, desired_content: form.elements.desired.value, avoided_content: form.elements.avoided.value, preferred_durations: durations }) });
      showToast("Creator Profile revision saved");
      await refresh();
    } catch (error) { showToast(error.message, true); }
  });
  root.append(form);
}

function renderReferencePicker(root) {
  state.referenceGeneration += 1;
  state.referenceSource = null;
  $("#reference-source-name").textContent = "Choose a source recording";
  $("#reference-player").pause();
  $("#reference-player").removeAttribute("src");
  $("#reference-media").classList.add("is-hidden");
  root.append(heading("Reference moments", "Blind annotation"));
  const list = document.createElement("div");
  list.className = "side-list";
  state.data.sources.forEach((source) => {
    const button = document.createElement("button");
    button.className = "side-item";
    button.textContent = source.original_name;
    button.addEventListener("click", () => renderReferenceForm(root, source));
    list.append(button);
  });
  root.append(list);
}

async function renderReferenceForm(root, source) {
  const generation = ++state.referenceGeneration;
  state.referenceSource = source;
  $("#reference-source-name").textContent = source.original_name;
  const referenceMedia = $("#reference-media");
  const referencePlayer = $("#reference-player");
  if (source.proxy_artifact_id) {
    referencePlayer.src = `/api/media/${source.proxy_artifact_id}`;
    referenceMedia.classList.remove("is-hidden");
  } else {
    referencePlayer.removeAttribute("src");
    referenceMedia.classList.add("is-hidden");
  }
  syncWorkspace();
  const data = await api(`/api/sources/${source.id}/references`);
  if (!referenceRequestIsCurrent(generation, source.id)) return;
  root.replaceChildren(heading("Reference moments", `${data.references.length}`));
  const form = document.createElement("form");
  form.className = "form-stack";
  form.innerHTML = `
    <label>Certainty<select name="certainty"><option value="definite">Definite</option><option value="possible">Possible</option></select></label>
    <label>Language slice<select name="language"><option value="unknown">Not labeled</option><option value="fi">Finnish</option><option value="en">English</option><option value="code_switched">Code-switched</option><option value="language_neutral">Language-neutral</option></select></label>
    <label>Category<select name="category">${["reaction", "comedy", "story", "opinion", "explanation"].map((value) => `<option value="${value}">${value}</option>`).join("")}</select></label>
    <div class="inline-fields"><label>Start seconds<input name="start" type="number" min="0" step="0.001" required></label><label>End seconds<input name="end" type="number" min="0" step="0.001" required></label></div>
    <label>Event seconds<input name="event" type="number" min="0" step="0.001" required></label>
    <label>Short-form suitability (0–4)<input name="suitability" type="number" min="0" max="4" value="3" required></label>
    <label>Rationale<textarea name="rationale" rows="3" required></textarea></label>
    <button class="button primary" type="submit">Save Reference Moment</button>`;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api(`/api/sources/${source.id}/references`, { method: "POST", body: JSON.stringify({ certainty: form.elements.certainty.value, language_slice: form.elements.language.value, category: form.elements.category.value, start_seconds: Number(form.elements.start.value), end_seconds: Number(form.elements.end.value), event_seconds: Number(form.elements.event.value), short_form_suitability: Number(form.elements.suitability.value), rationale: form.elements.rationale.value }) });
      if (!referenceRequestIsCurrent(generation, source.id)) return;
      showToast("Reference Moment saved without exposing system output");
      await renderReferenceForm(root, source);
    } catch (error) {
      if (referenceRequestIsCurrent(generation, source.id)) showToast(error.message, true);
    }
  });
  root.append(form);
  data.references.forEach((reference) => {
    const item = document.createElement("div");
    item.className = "side-item";
    const text = document.createElement("strong");
    text.textContent = `${reference.certainty} · ${reference.category} · ${reference.language_slice}`;
    const detail = document.createElement("small");
    detail.textContent = `${seconds(reference.start_us)}–${seconds(reference.end_us)} sec${reference.frozen ? " · frozen" : ""}`;
    item.append(text, detail);
    if (!reference.frozen) {
      const freeze = document.createElement("button");
      freeze.className = "button secondary";
      freeze.textContent = "Freeze revision";
      freeze.addEventListener("click", async () => {
        try {
          await api(`/api/references/${reference.annotation_set_id}/freeze`, { method: "POST", body: "{}" });
          if (!referenceRequestIsCurrent(generation, source.id)) return;
          await renderReferenceForm(root, source);
        } catch (error) {
          if (referenceRequestIsCurrent(generation, source.id)) showToast(error.message, true);
        }
      });
      item.append(freeze);
    }
    root.append(item);
  });
}

async function selectQueue(queueId) {
  try {
    state.queue = await api(`/api/queues/${queueId}`);
    state.proposalIndex = 0;
    markReviewInteraction();
    renderSidebar();
    renderProposal();
  } catch (error) { showToast(error.message, true); }
}

async function renderProposal() {
  const proposals = reviewProposals();
  if (!proposals.length) {
    $("#player").pause();
    state.stopAt = null;
    state.waveformGeneration += 1;
    $("#review-card").classList.add("is-hidden");
    $("#empty-state").classList.remove("is-hidden");
    return;
  }
  const proposal = proposals[state.proposalIndex];
  $("#empty-state").classList.add("is-hidden");
  $("#review-card").classList.remove("is-hidden");
  $("#proposal-meta").textContent = `${proposal.category} · rank ${proposal.rank}`;
  $("#proposal-summary").textContent = proposal.summary;
  $("#proposal-count").textContent = `${state.proposalIndex + 1} / ${proposals.length}`;
  $("#previous-proposal").disabled = state.proposalIndex === 0;
  $("#next-proposal").disabled = state.proposalIndex >= proposals.length - 1;
  $("#boundary-start").value = editableSeconds(proposal.current_decision?.boundary_start_us ?? proposal.start_us);
  $("#boundary-end").value = editableSeconds(proposal.current_decision?.boundary_end_us ?? proposal.end_us);
  const player = $("#player");
  player.src = `/api/media/${state.queue.snapshot.proxy_artifact_id}`;
  playInterval(Number($("#boundary-start").value), Number($("#boundary-end").value));

  const evidence = $("#evidence-list");
  evidence.replaceChildren();
  proposal.evidence.forEach((item) => {
    const box = document.createElement("div");
    box.className = "evidence-item";
    const text = document.createElement("div");
    text.textContent = item.content;
    const time = document.createElement("small");
    time.textContent = `${seconds(item.start_us)}–${seconds(item.end_us)} sec · ${item.evidence_type}`;
    box.append(text, time);
    evidence.append(box);
  });
  const judgments = $("#judgment-list");
  judgments.replaceChildren();
  Object.entries(proposal.judgments).forEach(([key, value]) => {
    const label = document.createElement("span");
    label.textContent = key.replaceAll("_", " ");
    const score = document.createElement("span");
    score.textContent = `${value} / 4`;
    judgments.append(label, score);
  });
  const structure = $("#structure-list");
  structure.replaceChildren();
  [["setup", proposal.setup_start_us], ["hook", proposal.hook_us], ["event", proposal.event_us], ["payoff", proposal.payoff_us], ["exit", proposal.exit_us]].forEach(([labelText, value]) => {
    if (value === null || value === undefined) return;
    const label = document.createElement("span");
    label.textContent = labelText;
    const time = document.createElement("span");
    time.textContent = `${seconds(value)} sec`;
    structure.append(label, time);
  });
  const reasons = $("#reason-list");
  reasons.replaceChildren();
  (proposal.reasons_against_selection || []).forEach((value) => {
    const line = document.createElement("div");
    line.className = "evidence-item";
    line.textContent = value;
    reasons.append(line);
  });
  if (!reasons.children.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "No model-recorded concerns.";
    reasons.append(empty);
  }
  const risk = $("#risk-panel");
  risk.replaceChildren();
  risk.classList.toggle("is-hidden", !proposal.risks.length);
  proposal.risks.forEach((item) => {
    const line = document.createElement("div");
    line.textContent = `${item.risk_kind}: ${item.reason}`;
    risk.append(line);
  });
  updateBoundaryActionState(proposal);
  const waveformGeneration = ++state.waveformGeneration;
  drawWaveform(state.queue.snapshot.source_recording_id, proposal.id, waveformGeneration);
  syncWorkspace();
}

function playInterval(start, end) {
  const player = $("#player");
  state.stopAt = end;
  player.currentTime = Math.max(0, start);
  player.play().catch(() => {});
}

$("#player").addEventListener("timeupdate", () => {
  if (state.stopAt !== null && $("#player").currentTime >= state.stopAt) {
    $("#player").pause();
    state.stopAt = null;
  }
});

$("#review-card").addEventListener("pointerdown", markReviewInteraction);
$("#review-card").addEventListener("input", markReviewInteraction);
$("#player").addEventListener("play", markReviewInteraction);

async function flushReviewActivity() {
  const now = Date.now();
  const elapsed = Math.min(15_000, Math.max(1, now - state.lastReviewTick));
  state.lastReviewTick = now;
  if (state.reviewFlushInFlight || document.visibilityState !== "visible" || state.view !== "review") return;
  if (!reviewProposals().length || $("#review-card").classList.contains("is-hidden")) return;
  const player = $("#player");
  const playback = !player.paused && !player.ended;
  const interaction = now - state.lastReviewInteraction <= 60_000;
  if (!playback && !interaction) return;
  const proposal = reviewProposals()[state.proposalIndex];
  const payload = state.pendingReviewActivity || {
    queue_snapshot_id: state.queue.snapshot.id,
    clip_proposal_id: proposal.id,
    session_id: state.reviewSession,
    sequence_number: state.reviewSequence,
    active_milliseconds: elapsed,
    activity_kind: playback ? "playback" : "interaction",
  };
  state.pendingReviewActivity = payload;
  state.reviewFlushInFlight = true;
  try {
    await api("/api/review-activity", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.reviewSequence += 1;
    state.pendingReviewActivity = null;
  } catch (_) {
    // Review remains usable if local activity telemetry cannot be persisted.
  } finally {
    state.reviewFlushInFlight = false;
  }
}

window.setInterval(flushReviewActivity, 5000);

async function drawWaveform(sourceId, proposalId, generation) {
  const payload = await api(`/api/sources/${sourceId}/waveform?bins=800`).catch(() => ({ bins: [] }));
  const current = reviewProposals()[state.proposalIndex];
  if (generation !== state.waveformGeneration || current?.id !== proposalId) return;
  state.waveformBins = payload.bins;
  paintWaveform();
}

function paintWaveform() {
  const canvas = $("#waveform");
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, canvas.clientWidth * ratio);
  canvas.height = 72 * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, canvas.clientWidth, 72);
  context.fillStyle = "#8cc5b2";
  const width = canvas.clientWidth / Math.max(1, state.waveformBins.length);
  state.waveformBins.forEach((value, index) => {
    const height = Math.max(1, value * 58);
    context.fillRect(index * width, (72 - height) / 2, Math.max(1, width), height);
  });
  const sourceId = state.queue?.snapshot?.source_recording_id;
  const source = state.data.sources.find((item) => item.id === sourceId);
  if (source) {
    context.fillStyle = "rgba(232, 215, 181, 0.33)";
    const startUs = Number($("#boundary-start").value) * 1_000_000;
    const endUs = Number($("#boundary-end").value) * 1_000_000;
    const x = (startUs / source.source_end_us) * canvas.clientWidth;
    const end = (endUs / source.source_end_us) * canvas.clientWidth;
    context.fillRect(x, 0, Math.max(2, end - x), 72);
  }
}

function boundaryInputChanged() {
  paintWaveform();
  const proposal = reviewProposals()[state.proposalIndex];
  if (proposal) updateBoundaryActionState(proposal);
}

$("#boundary-start").addEventListener("input", boundaryInputChanged);
$("#boundary-end").addEventListener("input", boundaryInputChanged);

async function submitDecision(decision, reason = null, note = "") {
  const proposal = reviewProposals()[state.proposalIndex];
  const prior = proposal.current_decision?.revision_number || 0;
  const startUs = Math.round(Number($("#boundary-start").value) * 1_000_000);
  const endUs = Math.round(Number($("#boundary-end").value) * 1_000_000);
  const boundaryChanged = startUs !== Number(proposal.start_us) || endUs !== Number(proposal.end_us);
  const body = {
    decision,
    idempotency_key: freshKey(),
    expected_prior_revision: prior,
    rejection_reason: reason,
    note,
  };
  if (decision !== "withdrawn" && boundaryChanged) {
    body.boundary_start_seconds = startUs / 1_000_000;
    body.boundary_end_seconds = endUs / 1_000_000;
  }
  try {
    await api(`/api/proposals/${proposal.id}/decisions`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.queue = await api(`/api/queues/${state.queue.snapshot.id}`);
    showToast(`Decision saved: ${decision}`);
    const remaining = reviewProposals();
    if (decision !== "withdrawn" && state.proposalIndex < remaining.length - 1) state.proposalIndex += 1;
    state.proposalIndex = Math.max(0, Math.min(state.proposalIndex, remaining.length - 1));
    renderProposal();
  } catch (error) { showToast(error.message, true); }
}

$("#accept").addEventListener("click", () => submitDecision("accept"));
$("#maybe").addEventListener("click", () => submitDecision("maybe"));
$("#undo").addEventListener("click", () => submitDecision("withdrawn"));
$("#reject").addEventListener("click", () => $("#reject-dialog").showModal());
$("#confirm-reject").addEventListener("click", (event) => {
  event.preventDefault();
  $("#reject-dialog").close();
  submitDecision("reject", $("#reject-reason").value, $("#reject-note").value);
});
$("#play-context").addEventListener("click", () => playInterval(Math.max(0, Number($("#boundary-start").value) - 10), Number($("#boundary-end").value) + 10));
function moveProposal(offset) {
  const proposals = reviewProposals();
  const nextIndex = Math.max(0, Math.min(state.proposalIndex + offset, proposals.length - 1));
  if (nextIndex === state.proposalIndex) return;
  $("#player").pause();
  state.stopAt = null;
  state.proposalIndex = nextIndex;
  markReviewInteraction();
  renderProposal();
}

$("#previous-proposal").addEventListener("click", () => moveProposal(-1));
$("#next-proposal").addEventListener("click", () => moveProposal(1));
$("#reanalyze-boundary").addEventListener("click", async () => {
  const proposal = reviewProposals()[state.proposalIndex];
  if (!boundariesMatchCurrentDecision(proposal)) {
    showToast("Save the decision before reanalyzing these boundaries", true);
    return;
  }
  if (!window.confirm("Re-evaluate this edited interval and create a successor proposal in a new immutable queue?")) return;
  try {
    await api(`/api/queues/${state.queue.snapshot.id}/proposals/${proposal.id}/reanalyze-boundary`, { method: "POST", body: "{}" });
    showToast("Boundary reanalysis started");
    await refresh();
  } catch (error) { showToast(error.message, true); }
});
$("#export").addEventListener("click", async () => {
  const proposal = reviewProposals()[state.proposalIndex];
  if (!boundariesMatchCurrentDecision(proposal)) {
    showToast("Save the decision before exporting these boundaries", true);
    return;
  }
  if (!window.confirm("Render this exact accepted interval from the original recording?")) return;
  const confirmedRisk = proposal.risks.length ? window.confirm("This proposal has Risk flags. Confirm that you reviewed them before export.") : false;
  if (proposal.risks.length && !confirmedRisk) return;
  const stale = Boolean(proposal.current_decision?.outside_evaluated_context);
  const confirmedStale = stale ? window.confirm("These boundaries extend beyond evaluated evidence. Export anyway?") : false;
  if (stale && !confirmedStale) return;
  try {
    await api(`/api/proposals/${proposal.id}/exports`, { method: "POST", body: JSON.stringify({ idempotency_key: freshKey(), confirmed: true, expected_decision_revision: proposal.current_decision.revision_number, confirmed_risk: confirmedRisk, confirmed_stale_coverage: confirmedStale }) });
    showToast("Export rendered and verified");
  } catch (error) { showToast(error.message, true); }
});

document.addEventListener("keydown", (event) => {
  markReviewInteraction();
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || $("#review-card").classList.contains("is-hidden")) return;
  const key = event.key.toLowerCase();
  if (key === "a") submitDecision("accept");
  if (key === "m") submitDecision("maybe");
  if (key === "r") $("#reject-dialog").showModal();
  if (key === "u") submitDecision("withdrawn");
  if (key === "arrowleft") moveProposal(-1);
  if (key === "arrowright") moveProposal(1);
});

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("is-active", item === tab));
  state.view = tab.dataset.view;
  renderSidebar();
}));

refresh().catch((error) => {
  $("#system-status").textContent = "Unavailable";
  showToast(error.message, true);
});
