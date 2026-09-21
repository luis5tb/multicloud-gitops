"""Embedded browser UI for the A2A endpoint."""

UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ericsson A2A agent</title>
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
    form { display: flex; gap: .5rem; margin-top: 1rem; }
    textarea { flex: 1; min-height: 4rem; padding: .6rem; }
    button { padding: .6rem 1rem; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Ericsson A2A agent</h1>
  <div id="status">Requests are forwarded to the configured downstream A2A agent.</div>
  <main id="messages" aria-live="polite"></main>
  <form id="chat-form">
    <textarea id="prompt" required placeholder="Send a request..."></textarea>
    <button type="submit">Send</button>
  </form>
  <script>
    const messages = document.getElementById('messages');
    const form = document.getElementById('chat-form');
    const prompt = document.getElementById('prompt');
    function addMessage(who, text) {
      const item = document.createElement('div');
      item.className = `message ${who}`;
      item.textContent = `${who === 'user' ? 'You' : 'Agent'}: ${text}`;
      messages.appendChild(item);
      messages.scrollTop = messages.scrollHeight;
    }
    function textFrom(value) {
      if (typeof value === 'string') return value;
      if (!value || typeof value !== 'object') return '';
      if (typeof value.text === 'string') return value.text;
      for (const key of ['message', 'artifact', 'artifacts', 'result', 'status', 'parts']) {
        if (value[key]) {
          const found = textFrom(value[key]);
          if (found) return found;
        }
      }
      if (Array.isArray(value)) return value.map(textFrom).filter(Boolean).join('\n');
      return '';
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
        addMessage('agent', textFrom(body.result) || JSON.stringify(body.result));
      } catch (error) {
        addMessage('agent', `Request failed: ${error.message}`);
      }
    });
  </script>
</body>
</html>
"""

