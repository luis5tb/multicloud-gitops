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
    .override textarea { min-height: 4rem; font-family: ui-monospace, monospace; font-size: .75em; }
    form { display: flex; gap: .5rem; margin-top: 1rem; }
    textarea { flex: 1; min-height: 4rem; padding: .6rem; }
    button { padding: .6rem 1rem; cursor: pointer; }
  </style>
</head>
<body>
  <h1>ACME A2A agent</h1>
  <div id="status">Requests are forwarded to the configured downstream A2A agent.
    Each message is independent (no saved conversation), so state the target
    OpenShift cluster API URL (e.g. https://api.example.com:6443) every time.</div>
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
      If a downstream agent asks for renewal, ACME mints a fresh token with the
      identity provider selected above, so paste a token from that same provider.</div>
  </div>
  <main id="messages" aria-live="polite"></main>
  <form id="chat-form">
    <textarea id="prompt" required placeholder="e.g. https://api.example.com:6443 -- then your request..."></textarea>
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
    providerSelect.addEventListener('change', updateAuthControls);
    readAuthOptions();
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
      // sources (OLS, for example, puts the identical final text in both
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
    //   * nested proto (what OLS's hand-rolled endpoint emits):
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
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const text = prompt.value.trim();
      if (!text) return;
      if (providerSelect.value === 'redhat' && tokenTypeSelect.value === 'user') {
        addMessage('agent', 'Request failed: Red Hat IdP user tokens are not implemented yet; select Agent token.');
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
      const payload = JSON.stringify({jsonrpc: '2.0', id, method: 'message/stream', params: {
        message: {messageId: id, role: 'user', parts: [{kind: 'text', text}]}
      }});
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
          const { thoughts, answer } = renderTaskResult(body.result);
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
            acc.ingest(envelope.result);
            const { thoughts, answer } = acc.current();
            agentMessage.update(thoughts, answer);
          }
          if (streamError) break;
        }
        if (streamError) throw streamError;
        const { thoughts, answer } = acc.current();
        agentMessage.update(thoughts, answer || '(the agent returned no answer)');
        await agentMessage.setToken(debugId);
      } catch (error) {
        addMessage('agent', `Request failed: ${error.message}`);
      }
    });
  </script>
</body>
</html>
"""

