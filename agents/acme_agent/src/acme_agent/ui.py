"""Embedded browser UI for the A2A endpoint."""

UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ACME A2A agent</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
    h1 { margin-bottom: .25rem; }
    #status { color: #777; margin-bottom: 1rem; }
    #messages { min-height: 280px; border: 1px solid #7778; border-radius: .5rem;
      padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere; }
    .message { margin: .75rem 0; }
    .user { color: #16803c; }
    .agent { color: #2869c7; }
    .thoughts { margin-bottom: .4rem; color: #888; font-style: italic; font-size: .9em; }
    .thoughts summary { cursor: pointer; color: #888; }
    .thoughts div { margin-top: .3rem; white-space: pre-wrap; }
    .token details { margin-bottom: .4rem; }
    .token summary { cursor: pointer; color: #888; }
    .token textarea { width: 100%; min-height: 6rem; margin-top: .3rem; padding: .4rem;
      font-family: ui-monospace, monospace; font-size: .75em; white-space: pre; }
    .token button { margin-top: .3rem; }
    .controls { display: flex; flex-wrap: wrap; gap: .75rem; margin: 0 0 1rem; }
    .controls label { display: grid; gap: .25rem; font-size: .85rem; color: #555; }
    .controls select { padding: .35rem; }
    .override { display: grid; gap: .35rem; margin: 0 0 1rem; font-size: .85rem; color: #555; }
    .conversation { display: flex; align-items: center; gap: .5rem; margin: 0 0 1rem;
      font-size: .85rem; color: #555; }
    .conversation button { padding: .3rem .7rem; }
    .notice { color: #888; font-style: italic; }
    .approval { border: 2px solid #d69e00; border-radius: .5rem; padding: 1rem;
      margin-top: 1rem; }
    .approval h2 { margin: 0 0 .5rem; font-size: 1.1rem; }
    .approval-options { margin: .5rem 0; }
    .approval-actions { display: flex; gap: .5rem; margin-top: .75rem; }
    .override textarea { min-height: 4rem; font-family: ui-monospace, monospace; font-size: .75em; }
    form { display: flex; gap: .5rem; margin-top: 1rem; }
    textarea { flex: 1; min-height: 4rem; padding: .6rem; }
    button { padding: .6rem 1rem; cursor: pointer; }
  </style>
</head>
<body>
  <h1>ACME A2A agent</h1>
  <div id="status">Requests are forwarded to the configured downstream A2A agent.
    Messages in one conversation share context, so later questions can refer to
    earlier answers.</div>
  <div class="conversation">
    <span id="conversation-label">New conversation</span>
    <button type="button" id="new-conversation">New conversation</button>
  </div>
  <div id="auth-controls" class="controls" hidden>
    <label>Identity provider
      <select id="auth-provider"></select>
    </label>
    <label>Red Hat SSO
      <select id="auth-environment"></select>
    </label>
    <label>Token
      <select id="auth-token-type">
        <option value="agent">Agent token</option>
        <option value="user">User token (not implemented)</option>
      </select>
    </label>
  </div>
  <div id="override-controls" class="override" hidden>
    <label><input type="checkbox" id="override-enabled">
      Use a pasted token for the next request (testing)</label>
    <textarea id="override-token" placeholder="Paste a bearer token, e.g. one copied from an earlier answer" hidden></textarea>
    <div>The pasted token replaces Token A for the first downstream call only.
      If a downstream agent asks for renewal, ACME mints a fresh token itself
      (with the identity provider selected above, when there is a choice), so
      paste a token from that same provider.</div>
  </div>
  <main id="messages" aria-live="polite"></main>
  <section id="approval-panel" class="approval" aria-live="polite" hidden>
    <h2>Approval required</h2>
    <div id="approval-prompt"></div>
    <ol id="approval-options" class="approval-options" hidden></ol>
    <label id="approval-choice-label" hidden>Option
      <select id="approval-choice"></select>
    </label>
    <div class="approval-actions">
      <button type="button" id="approve-button">Approve</button>
      <button type="button" id="reject-button">Reject</button>
    </div>
  </section>
  <form id="chat-form">
    <textarea id="prompt" required placeholder="Type your request..."></textarea>
    <button type="submit">Send</button>
  </form>
  <script id="auth-options" type="application/json">__AUTH_OPTIONS_JSON__</script>
  <script>
    const messages = document.getElementById('messages');
    const form = document.getElementById('chat-form');
    const prompt = document.getElementById('prompt');
    const authControls = document.getElementById('auth-controls');
    const providerSelect = document.getElementById('auth-provider');
    const environmentSelect = document.getElementById('auth-environment');
    const tokenTypeSelect = document.getElementById('auth-token-type');
    const overrideControls = document.getElementById('override-controls');
    const overrideEnabled = document.getElementById('override-enabled');
    const overrideToken = document.getElementById('override-token');
    const approvalPanel = document.getElementById('approval-panel');
    const approvalPrompt = document.getElementById('approval-prompt');
    const approvalOptions = document.getElementById('approval-options');
    const approvalChoiceLabel = document.getElementById('approval-choice-label');
    const approvalChoice = document.getElementById('approval-choice');
    const approveButton = document.getElementById('approve-button');
    const rejectButton = document.getElementById('reject-button');
    let tokenDebugEnabled = false;
    overrideEnabled.addEventListener('change', () => {
      overrideToken.hidden = !overrideEnabled.checked;
    });
    function readAuthOptions() {
      let options = {};
      try { options = JSON.parse(document.getElementById('auth-options').textContent); }
      catch (error) { console.warn('Ignoring invalid auth options', error); }
      tokenDebugEnabled = !!options.tokenDebugEnabled;
      overrideControls.hidden = !options.tokenOverrideEnabled;
      if (!options.selectionEnabled) return;
      providerSelect.replaceChildren();
      const keycloak = document.createElement('option');
      keycloak.value = 'keycloak';
      keycloak.textContent = 'Keycloak';
      providerSelect.appendChild(keycloak);
      if (options.redHatEnabled) {
        const redhat = document.createElement('option');
        redhat.value = 'redhat';
        redhat.textContent = 'Red Hat IdP';
        providerSelect.appendChild(redhat);
      }
      providerSelect.value = options.defaultProvider === 'redhat' && options.redHatEnabled
        ? 'redhat' : 'keycloak';
      environmentSelect.replaceChildren();
      (options.environments || []).forEach((environment) => {
        const option = document.createElement('option');
        option.value = environment.id;
        option.textContent = environment.label;
        option.disabled = !environment.available;
        environmentSelect.appendChild(option);
      });
      const defaultEnvironment = (options.environments || []).find(
        (environment) => environment.id === options.defaultEnvironment && environment.available
      );
      const firstAvailableEnvironment = (options.environments || []).find(
        (environment) => environment.available
      );
      environmentSelect.value = (defaultEnvironment || firstAvailableEnvironment || {}).id || 'production';
      tokenTypeSelect.value = options.defaultTokenType === 'user' ? 'user' : 'agent';
      authControls.hidden = false;
      updateAuthControls();
    }
    function updateAuthControls() {
      const redhat = providerSelect.value === 'redhat';
      environmentSelect.disabled = !redhat;
      tokenTypeSelect.disabled = !redhat;
    }
    // One conversation = one A2A contextId. ACME keys its ADK session by the
    // contextId, and ADK's RemoteA2aAgent then reuses the downstream (MAS)
    // context it got back, so history is kept on both sides. Kept in
    // sessionStorage so a page reload stays in the same conversation; ACME's
    // sessions are in memory, so an ACME restart starts over on the server.
    const conversationLabel = document.getElementById('conversation-label');
    let contextId = sessionStorage.getItem('acme-context-id') || '';
    let taskId = sessionStorage.getItem('acme-task-id') || '';
    let taskState = sessionStorage.getItem('acme-task-state') || '';
    let approvalCall = null;
    try { approvalCall = JSON.parse(sessionStorage.getItem('acme-approval-call') || 'null'); }
    catch (error) { sessionStorage.removeItem('acme-approval-call'); }
    function showConversation() {
      conversationLabel.textContent = contextId
        ? `Conversation ${contextId.slice(0, 8)}`
        : 'New conversation';
    }
    function setContextId(value) {
      if (!value || value === contextId) return;
      contextId = value;
      sessionStorage.setItem('acme-context-id', contextId);
      showConversation();
    }
    function setTaskId(value) {
      if (!value || value === taskId) return;
      taskId = value;
      sessionStorage.setItem('acme-task-id', taskId);
    }
    function clearTaskId(resetState = true) {
      taskId = '';
      approvalCall = null;
      sessionStorage.removeItem('acme-task-id');
      if (resetState) {
        taskState = '';
        sessionStorage.removeItem('acme-task-state');
      }
      sessionStorage.removeItem('acme-approval-call');
      updateApprovalPanel('');
    }
    function normalizedTaskState(state) {
      return String(state || '').toLowerCase().replace(/^task_state_/, '').replace(/_/g, '-');
    }
    function isInputRequired() { return normalizedTaskState(taskState) === 'input-required'; }
    function isTerminalTaskState(state) {
      return ['completed', 'failed', 'canceled', 'rejected'].includes(normalizedTaskState(state));
    }
    function findApprovalCall(node) {
      if (!node || typeof node !== 'object') return null;
      if (Array.isArray(node)) {
        for (const item of node) {
          const found = findApprovalCall(item);
          if (found) return found;
        }
        return null;
      }
      const data = node.data;
      const type = node.metadata && (node.metadata.adk_type || node.metadata.type);
      if (data && typeof data === 'object' &&
          (type === 'function_call' || String(data.name || '').includes('mock_function_call_for_required_user_input'))) {
        return data;
      }
      for (const [key, value] of Object.entries(node)) {
        if (key === 'metadata' || key === 'data') continue;
        const found = findApprovalCall(value);
        if (found) return found;
      }
      return null;
    }
    function captureTaskInfo(result) {
      if (!result || typeof result !== 'object') return;
      const task = result.task || (result.kind === 'task' ? result : null);
      const statusUpdate = result.statusUpdate || result.status_update ||
        (result.kind === 'status-update' || result.status ? result : null);
      const snapshot = task || result;
      const id = (task && task.id) || (statusUpdate && (statusUpdate.taskId || statusUpdate.task_id)) || result.taskId;
      const context = (task && task.contextId) || (statusUpdate && (statusUpdate.contextId || statusUpdate.context_id));
      if (id) setTaskId(id);
      if (context) setContextId(context);
      const status = (statusUpdate && statusUpdate.status) || (snapshot && snapshot.status);
      if (status && status.state) {
        taskState = status.state;
        sessionStorage.setItem('acme-task-state', taskState);
        if (isTerminalTaskState(taskState)) {
          clearTaskId(false);
          return;
        }
      }
      const call = findApprovalCall(result);
      if (call) {
        approvalCall = call;
        sessionStorage.setItem('acme-approval-call', JSON.stringify(call));
      }
    }
    function startNewConversation(reason) {
      const hadConversation = !!contextId;
      contextId = '';
      sessionStorage.removeItem('acme-context-id');
      clearTaskId();
      showConversation();
      if (hadConversation) {
        const note = document.createElement('div');
        note.className = 'message notice';
        note.textContent = reason || 'New conversation started.';
        messages.appendChild(note);
        messages.scrollTop = messages.scrollHeight;
      }
    }
    // Pulls the contextId out of any A2A result shape: a task, a message, or a
    // status/artifact update, in either the 1.0 (nested) or 0.3 (flat) form.
    function captureContextId(result) {
      if (!result || typeof result !== 'object') return;
      const nested = result.task || result.statusUpdate || result.artifactUpdate || result.message;
      setContextId((nested && nested.contextId) || result.contextId);
      captureTaskInfo(result);
    }
    function findOptions(value) {
      if (!value || typeof value !== 'object') return null;
      if (Array.isArray(value)) return null;
      for (const [key, nested] of Object.entries(value)) {
        if (key.toLowerCase() === 'options' && Array.isArray(nested)) return nested;
      }
      for (const nested of Object.values(value)) {
        const found = findOptions(nested);
        if (found) return found;
      }
      return null;
    }
    function optionLabel(option, index) {
      if (typeof option === 'string') return option;
      if (option && typeof option === 'object') {
        const label = option.label || option.name || option.title || option.description;
        if (label) return option.summary ? `${label}: ${option.summary}` : String(label);
      }
      return JSON.stringify(option);
    }
    function updateApprovalPanel(statusText) {
      if (!isInputRequired()) {
        approvalPanel.hidden = true;
        return;
      }
      approvalPanel.hidden = false;
      const payload = approvalCall && approvalCall.args && approvalCall.args.input_required;
      const visiblePrompt = typeof statusText === 'string' && statusText.trim()
        ? statusText.trim()
        : typeof payload === 'string' ? payload : (payload && payload.prompt) || '';
      const details = payload && typeof payload === 'object'
        ? JSON.stringify(payload, null, 2) : '';
      approvalPrompt.textContent = visiblePrompt || details ||
        'The downstream agent is waiting for your approval or response.';
      const options = findOptions(payload) || [];
      approvalOptions.replaceChildren();
      approvalChoice.replaceChildren();
      options.forEach((option, index) => {
        const label = optionLabel(option, index);
        const item = document.createElement('li');
        item.textContent = label;
        approvalOptions.appendChild(item);
        const choice = document.createElement('option');
        choice.value = String(option && Number.isInteger(option.index) ? option.index : index);
        choice.textContent = `${(option && option.number) || index + 1}) ${label}`;
        approvalChoice.appendChild(choice);
      });
      approvalOptions.hidden = options.length === 0;
      approvalChoiceLabel.hidden = options.length === 0;
      approveButton.disabled = !approvalCall;
      rejectButton.disabled = !approvalCall;
    }
    document.getElementById('new-conversation').addEventListener('click', () => startNewConversation());
    // A downstream conversation belongs to one principal, so switching the
    // identity provider (or Red Hat SSO environment) starts a new one.
    function onIdentityChange() {
      startNewConversation('Identity changed; started a new conversation.');
    }
    providerSelect.addEventListener('change', () => { updateAuthControls(); onIdentityChange(); });
    environmentSelect.addEventListener('change', onIdentityChange);
    readAuthOptions();
    showConversation();
    updateApprovalPanel('');
    // Renders the agent's final answer prominently and, if present, its
    // reasoning/"thinking" text in a separate, visually muted, collapsible
    // block above it -- the two are never concatenated into one line.
    function addMessage(who, text, thoughts) {
      const item = document.createElement('div');
      item.className = `message ${who}`;
      if (thoughts && thoughts.length) {
        const details = document.createElement('details');
        details.className = 'thoughts';
        const summary = document.createElement('summary');
        summary.textContent = 'Thinking';
        details.appendChild(summary);
        const body = document.createElement('div');
        body.textContent = thoughts.join('\n\n');
        details.appendChild(body);
        item.appendChild(details);
      }
      const answer = document.createElement('div');
      answer.textContent = `${who === 'user' ? 'You' : 'Agent'}: ${text}`;
      item.appendChild(answer);
      messages.appendChild(item);
      messages.scrollTop = messages.scrollHeight;
      return item;
    }
    function addTokenSection(item, token, metadata) {
      const details = document.createElement('details');
      details.className = 'token';
      const summary = document.createElement('summary');
      summary.textContent = `Access token (${metadata.provider}` +
        (metadata.environment ? `, ${metadata.environment}` : '') +
        `, ${metadata.tokenType})`;
      details.appendChild(summary);
      const body = document.createElement('div');
      const field = document.createElement('textarea');
      field.readOnly = true;
      field.value = token;
      field.setAttribute('aria-label', 'Access token');
      body.appendChild(field);
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.textContent = 'Copy token';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(token);
          copy.textContent = 'Copied';
          setTimeout(() => { copy.textContent = 'Copy token'; }, 1500);
        } catch (error) {
          copy.textContent = 'Copy failed; select manually';
        }
      });
      body.appendChild(copy);
      details.appendChild(body);
      item.appendChild(details);
      messages.scrollTop = messages.scrollHeight;
    }
    async function attachTokenDebug(item, debugId) {
      if (!tokenDebugEnabled || !debugId) return;
      try {
        const response = await fetch(`/ui/auth/token/${encodeURIComponent(debugId)}`, {
          credentials: 'same-origin'
        });
        if (!response.ok) return;
        const debug = await response.json();
        if (debug.accessToken) addTokenSection(item, debug.accessToken, debug);
      } catch (error) {
        console.warn('Token debug retrieval failed', error);
      }
    }
    // Walks one A2A Message/Part/array node, bucketing each text part's
    // content into `thoughts` (ADK's own adk_thought/is_thought part
    // metadata -- reasoning, not the answer) or `answerParts` (everything
    // else). This is a structural parse of the real A2A shape, not a
    // generic "find the first text anywhere" search: it is the per-part
    // metadata tag, not accidental ordering, that decides which bucket a
    // given piece of text lands in.
    function collectParts(node, thoughts, answerParts) {
      if (!node) return;
      if (Array.isArray(node)) {
        node.forEach((item) => collectParts(item, thoughts, answerParts));
        return;
      }
      if (typeof node !== 'object') return;
      if (typeof node.text === 'string') {
        const isThought = !!(node.metadata && (node.metadata.adk_thought || node.metadata.is_thought));
        (isThought ? thoughts : answerParts).push(node.text);
        return;
      }
      if (Array.isArray(node.parts)) collectParts(node.parts, thoughts, answerParts);
    }
    // Extracts { thoughts, answer } from a SendMessage result. Prefers the
    // task's authoritative terminal message (status.message); falls back to
    // artifacts, then to the last history entry carrying any non-thought
    // text, for shapes where the terminal answer isn't under status.message.
    function renderTaskResult(result) {
      const task = (result && (result.task || result)) || {};
      const thoughts = [];
      const ignored = [];
      // Thoughts can appear anywhere (status.message, artifacts, or
      // history) -- collect them from everywhere so they're visible
      // regardless of which source the final answer itself came from.
      collectParts(task.status && task.status.message, thoughts, ignored);
      collectParts(task.artifacts, thoughts, ignored);
      collectParts(task.history, thoughts, ignored);

      // The answer comes from exactly one authoritative source, tried in
      // priority order, so the same text is never duplicated across
      // sources (some agents put the identical final text in both
      // status.message and artifacts).
      const answerParts = [];
      if (task.status && task.status.message) collectParts(task.status.message, ignored, answerParts);
      if (!answerParts.length && Array.isArray(task.artifacts)) collectParts(task.artifacts, ignored, answerParts);
      if (!answerParts.length && Array.isArray(task.history)) {
        for (let i = task.history.length - 1; i >= 0; i--) {
          const before = answerParts.length;
          collectParts(task.history[i], ignored, answerParts);
          if (answerParts.length > before) break;
        }
      }

      return {
        thoughts: thoughts.map((t) => t.trim()).filter(Boolean),
        answer: answerParts.join('\n').trim(),
      };
    }
    // Creates one agent message bubble and returns an updater that re-renders
    // its answer (and lazily its "Thinking" block) as streamed events arrive.
    // The bubble is only attached on the first update, so a request that errors
    // before producing anything leaves no empty bubble behind.
    function addUpdatableAgentMessage() {
      const item = document.createElement('div');
      item.className = 'message agent';
      const answer = document.createElement('div');
      item.appendChild(answer);
      let details, thoughtsBody, tokenDetails, attached = false;
      function update(thoughts, text) {
        if (!attached) { messages.appendChild(item); attached = true; }
        if (thoughts && thoughts.length) {
          if (!details) {
            details = document.createElement('details');
            details.className = 'thoughts';
            const summary = document.createElement('summary');
            summary.textContent = 'Thinking';
            details.appendChild(summary);
            thoughtsBody = document.createElement('div');
            details.appendChild(thoughtsBody);
            item.insertBefore(details, answer);
          }
          thoughtsBody.textContent = thoughts.join('\n\n');
        }
        answer.textContent = `Agent: ${text}`;
        messages.scrollTop = messages.scrollHeight;
      }
      async function setToken(debugId) {
        if (!debugId) return;
        if (!attached) { messages.appendChild(item); attached = true; }
        if (!tokenDetails) {
          tokenDetails = document.createElement('div');
          item.appendChild(tokenDetails);
        }
        await attachTokenDebug(tokenDetails, debugId);
      }
      return {update, setToken};
    }
    // Accumulates the streamed A2A events (message/stream) into a running
    // { thoughts, answer }. Each SSE event's `result` is a status-update, an
    // artifact-update, a Task snapshot, or a bare Message -- and it comes in one
    // of two shapes:
    //   * flattened a2a JSON (what ADK's to_a2a / the acme server emit):
    //       {kind:"status-update", status:{message:{parts:[...]}}, final:...}
    //       {kind:"artifact-update", artifact:{parts:[...]}, append:bool, lastChunk:bool}
    //   * nested proto (what a hand-rolled endpoint may emit):
    //       {statusUpdate:{status:{message:...}}}  {artifactUpdate:{artifact:...}}
    // Both are handled. The final answer is the "answer" artifact: a terminal
    // artifact-update (append:false) carries the whole text, and the terminal
    // status.message carries it too; we keep both and prefer the artifact.
    function makeAccumulator() {
      const thoughts = [];
      let statusAnswer = '';
      const artifactParts = [];
      function addThoughts(list) { list.forEach((t) => thoughts.push(t)); }
      function ingest(result) {
        if (!result || typeof result !== 'object') return;
        const kind = result.kind;
        // status-update: nested result.statusUpdate, or flattened (result.status).
        const su = result.statusUpdate
          || (kind === 'status-update' || result.status ? result : null);
        // artifact-update: nested result.artifactUpdate, or flattened (result.artifact).
        const au = result.artifactUpdate
          || (kind === 'artifact-update' || result.artifact ? result : null);
        if (au) {
          const t = [], a = [];
          collectParts(au.artifact, t, a);
          addThoughts(t);
          // append:false (or a fresh/last chunk) replaces; append:true extends.
          if (au.append === false) artifactParts.length = 0;
          a.forEach((x) => artifactParts.push(x));
          return;
        }
        if (su) {
          const msg = su.status && su.status.message;
          if (msg) {
            const t = [], a = [];
            collectParts(msg, t, a);
            addThoughts(t);
            // Latest message wins: the terminal status carries the full answer,
            // so replacing (not first-wins) ends on the complete text.
            if (a.length) statusAnswer = a.join('\n');
          }
          return;
        }
        // Task snapshot or a bare Message: reuse the single-result extractor.
        const { thoughts: tt, answer } = renderTaskResult(result);
        addThoughts(tt);
        if (answer) statusAnswer = answer;
      }
      function current() {
        // Prefer the accumulated "answer" artifact (the terminal full text);
        // fall back to the latest status.message.
        const answer = (artifactParts.join('') || statusAnswer).trim();
        const seen = new Set();
        const uniqThoughts = thoughts
          .map((t) => t.trim())
          .filter(Boolean)
          .filter((t) => (seen.has(t) ? false : (seen.add(t), true)));
        return { thoughts: uniqThoughts, answer };
      }
      return { ingest, current };
    }
    async function sendMessage(text) {
      text = String(text || '').trim();
      if (!text) return;
      if (providerSelect.value === 'redhat' && tokenTypeSelect.value === 'user') {
        addMessage('agent', 'Request failed: Red Hat IdP user tokens are not implemented yet; select Agent token.');
        return;
      }
      if (isInputRequired() && (!taskId || !approvalCall)) {
        addMessage('agent', 'Request failed: the pending approval response is not available. Start a new conversation and try again.');
        return;
      }
      prompt.value = '';
      addMessage('user', text);
      const id = crypto.randomUUID();
      const debugId = tokenDebugEnabled ? crypto.randomUUID() : '';
      // Stream (message/stream) rather than message/send: a long investigation
      // can take minutes, and a single non-streaming response holds one idle
      // connection open the whole time, which an intermediary (e.g. a cloud
      // load balancer's ~60s idle timeout, independent of the Route timeout)
      // silently drops -- surfacing as "NetworkError" even though the agent
      // eventually produced a full answer. Streaming keeps bytes flowing so the
      // idle timer never trips, and lets the answer render as it arrives.
      const message = {messageId: id, role: 'user', parts: [{kind: 'text', text}]};
      if (contextId) message.contextId = contextId;
      if (isInputRequired()) {
        // Keep the existing ACME task/context identifiers, but send the
        // caller's decision as plain text. The root before_model callback
        // routes it to the paused downstream task without resuming the ADK
        // long-running function invocation.
        message.taskId = taskId;
        message.parts = [{kind: 'text', text}];
      }
      const payload = JSON.stringify({jsonrpc: '2.0', id, method: 'message/stream', params: {message}});
      const headers = {
        'content-type': 'application/json',
        'accept': 'text/event-stream',
        'x-request-id': id
      };
      if (!authControls.hidden) {
        headers['x-acme-auth-provider'] = providerSelect.value;
        if (providerSelect.value === 'redhat') {
          headers['x-acme-auth-environment'] = environmentSelect.value;
          headers['x-acme-auth-token-type'] = tokenTypeSelect.value;
        }
      }
      if (debugId) headers['x-acme-token-debug-id'] = debugId;
      if (!overrideControls.hidden && overrideEnabled.checked) {
        const pasted = overrideToken.value.trim().replace(/^Bearer\s+/i, '');
        if (!pasted) {
          addMessage('agent', 'Request failed: paste a token or clear "Use a pasted token".');
          return;
        }
        headers['x-acme-auth-token'] = pasted;
      }
      try {
        const response = await fetch('/', {
          method: 'POST',
          headers,
          body: payload
        });
        const contentType = response.headers.get('content-type') || '';
        // Fallback: a server/build that doesn't stream returns a single JSON
        // envelope -- handle it exactly as before.
        if (!response.body || !contentType.includes('text/event-stream')) {
          const body = await response.json();
          if (!response.ok || body.error) throw new Error(JSON.stringify(body.error || body));
          captureContextId(body.result);
          const { thoughts, answer } = renderTaskResult(body.result);
          updateApprovalPanel(answer);
          const item = addMessage('agent', answer || JSON.stringify(body.result), thoughts);
          await attachTokenDebug(item, debugId);
          return;
        }
        if (!response.ok) {
          let message = `HTTP ${response.status}`;
          try {
            const body = await response.json();
            message = body.detail || body.error?.message || JSON.stringify(body);
          } catch (error) { /* keep the HTTP status */ }
          throw new Error(message);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const acc = makeAccumulator();
        const agentMessage = addUpdatableAgentMessage();
        let buffer = '';
        let streamError = null;
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          // Strip CRs so SSE framing is uniformly '\n\n' regardless of CRLF.
          buffer += decoder.decode(value, {stream: true}).replace(/\r/g, '');
          let sep;
          while ((sep = buffer.indexOf('\n\n')) >= 0) {
            const rawEvent = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const data = rawEvent
              .split('\n')
              .filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trim())
              .join('\n');
            if (!data || data === '[DONE]') continue;
            let envelope;
            try { envelope = JSON.parse(data); } catch (e) { continue; }
            if (envelope.error) { streamError = new Error(JSON.stringify(envelope.error)); break; }
            captureContextId(envelope.result);
            acc.ingest(envelope.result);
            const { thoughts, answer } = acc.current();
            agentMessage.update(thoughts, answer);
            updateApprovalPanel(answer);
          }
          if (streamError) break;
        }
        if (streamError) throw streamError;
        const { thoughts, answer } = acc.current();
        agentMessage.update(thoughts, answer || '(the agent returned no answer)');
        updateApprovalPanel(answer);
        await agentMessage.setToken(debugId);
      } catch (error) {
        addMessage('agent', `Request failed: ${error.message}`);
      }
    }
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await sendMessage(prompt.value);
    });
    approveButton.addEventListener('click', () => {
      const optionIndex = approvalChoiceLabel.hidden ? '' : approvalChoice.value;
      sendMessage(optionIndex === '' ? 'approve' : `approve option ${Number(optionIndex) + 1}`);
    });
    rejectButton.addEventListener('click', () => sendMessage('reject'));
  </script>
</body>
</html>
"""
