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
  <main id="messages" aria-live="polite"></main>
  <form id="chat-form">
    <textarea id="prompt" required placeholder="e.g. https://api.example.com:6443 -- then your request..."></textarea>
    <button type="submit">Send</button>
  </form>
  <script>
    const messages = document.getElementById('messages');
    const form = document.getElementById('chat-form');
    const prompt = document.getElementById('prompt');
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
      let details, thoughtsBody, attached = false;
      return function update(thoughts, text) {
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
      };
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
      prompt.value = '';
      addMessage('user', text);
      const id = crypto.randomUUID();
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
      try {
        const response = await fetch('/', {
          method: 'POST',
          headers: {'content-type': 'application/json', 'accept': 'text/event-stream'},
          body: payload
        });
        const contentType = response.headers.get('content-type') || '';
        // Fallback: a server/build that doesn't stream returns a single JSON
        // envelope -- handle it exactly as before.
        if (!response.body || !contentType.includes('text/event-stream')) {
          const body = await response.json();
          if (!response.ok || body.error) throw new Error(JSON.stringify(body.error || body));
          const { thoughts, answer } = renderTaskResult(body.result);
          addMessage('agent', answer || JSON.stringify(body.result), thoughts);
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const acc = makeAccumulator();
        const update = addUpdatableAgentMessage();
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
            update(thoughts, answer);
          }
          if (streamError) break;
        }
        if (streamError) throw streamError;
        const { thoughts, answer } = acc.current();
        update(thoughts, answer || '(the agent returned no answer)');
      } catch (error) {
        addMessage('agent', `Request failed: ${error.message}`);
      }
    });
  </script>
</body>
</html>
"""

