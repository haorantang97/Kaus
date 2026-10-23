/** Kaus module channel v1. Bundle this file with your local static module. */
export function connectKaus(moduleId, { timeoutMs = 15000 } = {}) {
  let port, closed = false, sequence = 0;
  const pending = new Map();
  let rejectReady;
  let detach = () => {};
  const ready = new Promise((resolve, reject) => {
    rejectReady = reject;
    const timer = setTimeout(() => { window.removeEventListener('message', receive); reject(new Error('Module connection timed out')); }, timeoutMs);
    function receive(event) {
      if (event.source !== parent || closed) return;
      if (event.data?.type === 'kaus:module:probe' && event.data.version === 1) announce();
      if (event.data?.type !== 'kaus:module:connect' || event.data.version !== 1 || event.data.moduleId !== moduleId || event.ports?.length !== 1 || port) return;
      clearTimeout(timer); window.removeEventListener('message', receive);
      port = event.ports[0];
      port.onmessage = ({ data }) => {
        const task = pending.get(data?.id);
        if (!task) return;
        pending.delete(data.id); clearTimeout(task.timer);
        if (data.error) task.reject(Object.assign(new Error(data.error.message), { code: data.error.code }));
        else task.resolve(data.result);
      };
      port.start(); resolve();
    }
    window.addEventListener('message', receive);
    detach = () => { clearTimeout(timer); window.removeEventListener('message', receive); window.removeEventListener('load', announce); };
  });
  function announce() { if (!closed) parent.postMessage({ type: 'kaus:module:ready', version: 1 }, '*'); }
  if (document.readyState === 'complete') setTimeout(announce, 0);
  else window.addEventListener('load', announce, { once: true });
  async function request(method, params = {}) {
    await ready;
    if (closed) throw new Error('Module connection closed');
    const id = `${moduleId}-${++sequence}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('Request timed out; check job status before retrying')); }, 45000);
      pending.set(id, { resolve, reject, timer }); port.postMessage({ id, method, params });
    });
  }
  return {
    request,
    context: () => request('context.get'),
    jobs: {
      start: (prompt, idempotencyKey = crypto.randomUUID()) => request('jobs.start', { prompt, idempotencyKey }),
      get: jobId => request('jobs.get', { jobId }),
      list: () => request('jobs.list'),
      cancel: jobId => request('jobs.cancel', { jobId }),
      resume: jobId => request('jobs.resume', { jobId }),
    },
    data: { get: key => request('data.get', { key }), set: (key, value) => request('data.set', { key, value }) },
    workflow: {
      get: () => request('workflow.get'),
      save: workflow => request('workflow.save', { workflow }),
      run: (input, idempotencyKey = crypto.randomUUID()) => request('workflow.run', { input, idempotencyKey }),
    },
    backend: (method, path, body) => request('backend.request', { method, path, ...(body === undefined ? {} : { body }) }),
    configure: () => request('ui.configure'),
    confirm: message => request('ui.confirm', { message }),
    copy: text => request('clipboard.writeText', { text }),
    openExternal: url => request('navigation.openExternal', { url }),
    openConversation: conversationId => request('navigation.openConversation', { conversationId }),
    close() {
      closed = true; detach(); rejectReady(new Error('Module connection closed')); port?.close();
      for (const task of pending.values()) { clearTimeout(task.timer); task.reject(new Error('Module connection closed')); }
      pending.clear();
    },
  };
}
