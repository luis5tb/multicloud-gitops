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
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const text = prompt.value.trim();
      if (!text) return;
      prompt.value = '';
      addMessage('user', text);
      const id = crypto.randomUUID();
      try {
        const response = await fetch('/', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({jsonrpc: '2.0', id, method: 'message/send', params: {
            message: {messageId: id, role: 'user', parts: [{kind: 'text', text}]}
          }})
        });
        const body = await response.json();
        if (!response.ok || body.error) throw new Error(JSON.stringify(body.error || body));
        const { thoughts, answer } = renderTaskResult(body.result);
        addMessage('agent', answer || JSON.stringify(body.result), thoughts);
      } catch (error) {
        addMessage('agent', `Request failed: ${error.message}`);
      }
    });
  </script>
</body>
</html>
"""

