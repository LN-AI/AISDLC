// Client-side error logging. Reports go to /api/client-log so failures show up
// in the server logs, but nothing sensitive ever leaves the browser: no tokens,
// no query strings, no request/response bodies — only a message, an optional
// stack trace, and where it happened. Keep it that way when extending this.
(function () {
  const ENDPOINT = '/api/client-log';
  const MAX_PER_SESSION = 25; // stop hammering the backend if something loops
  let sent = 0;
  // Set once the page is going away, which is the only moment a report needs to survive
  // teardown. See the note in `send()`.
  let unloading = false;
  window.addEventListener('pagehide', () => { unloading = true; });
  window.addEventListener('beforeunload', () => { unloading = true; });

  function redact(str) {
    if (!str) return str;
    return String(str)
      .replace(/Bearer\s+[A-Za-z0-9\-_.]+/gi, 'Bearer [redacted]')
      .replace(/([?&](token|key|password|secret|authorization)=)[^&\s]+/gi, '$1[redacted]');
  }

  function send(level, message, extra) {
    if (sent >= MAX_PER_SESSION) return;
    sent++;
    const payload = {
      level,
      message: redact(String(message == null ? '' : message)).slice(0, 2000),
      source: (extra && extra.source) || undefined,
      url: redact(location.pathname),
      line: (extra && extra.line) || undefined,
      col: (extra && extra.col) || undefined,
      stack: extra && extra.stack ? redact(String(extra.stack)).slice(0, 2000) : undefined,
    };
    const body = JSON.stringify(payload);
    try {
      // `keepalive: true` used to be set on every report. It is the wrong tool for routine
      // logging: Chromium services keepalive fetches through a separate pipeline with a hard
      // cap on concurrent requests and a 64KB *total* in-flight body budget shared across the
      // page, so a burst of error reports can start failing outright — and a logger that
      // drops reports under load is a logger that goes quiet exactly when it is needed.
      //
      // What `keepalive` was actually there for is the last report before the page goes away,
      // and `sendBeacon` is the API designed for that one case — so it is used there and only
      // there.
      if (unloading && navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }));
        return;
      }
      fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      }).catch(() => {});
    } catch { /* logging must never break the app */ }
  }

  window.feLog = {
    error(message, extra) { console.error(message); send('error', message, extra); },
    warn(message, extra) { console.warn(message); send('warn', message, extra); },
    info(message, extra) { send('info', message, extra); },
  };

  window.addEventListener('error', (e) => {
    feLog.error(e.message || 'Uncaught error', {
      source: 'window.onerror', line: e.lineno, col: e.colno,
      stack: e.error && e.error.stack,
    });
  });

  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason;
    feLog.error((reason && reason.message) || String(reason) || 'Unhandled promise rejection', {
      source: 'unhandledrejection', stack: reason && reason.stack,
    });
  });
})();
