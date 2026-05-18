// PipelineStatus.jsx — thin top status strip
// Pills for BOT / WS / INGEST / EXEC / KILLSWITCH + UTC clock + latency.
// Registers on window.Portfolio.PipelineStatus.
//
// Props:
//   pipeline: { bot, ws, ingest_live, ingest_total, exec_mode, killswitch, latency_ms }
//   loading:  bool — shows muted skeleton pills if true
//
// Background pulses pink when anything is unhealthy.

(function () {
  const { useEffect, useState } = React;
  const Pill = (window.UI && window.UI.Pill) || (({ text, variant }) =>
    React.createElement('span', { className: 'pill pill-' + (variant || 'muted') }, text));

  const utcClock = () => {
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + ' UTC';
  };

  const variantFor = {
    bot:        v => v === 'running' ? 'success' : (v === 'stopped' ? 'warning' : 'danger'),
    ws:         v => v === 'live'    ? 'success' : (v === 'reconnecting' ? 'warning' : 'danger'),
    ingest:     (live, total) => (live > 0 ? 'success' : 'danger'),
    exec:       v => v === 'LIVE'    ? 'success' : (v === 'PAPER' ? 'info' : 'warning'),
    killswitch: v => v === 'on'      ? 'danger'  : 'muted',
  };

  const PipelineStatus = ({ pipeline = {}, loading = false }) => {
    const [now, setNow] = useState(utcClock());
    useEffect(() => {
      const id = setInterval(() => setNow(utcClock()), 1000);
      return () => clearInterval(id);
    }, []);

    const bot        = (pipeline.bot       || 'unknown').toLowerCase();
    const ws         = (pipeline.ws        || 'unknown').toLowerCase();
    const liveMk     = pipeline.ingest_live  ?? 0;
    const totalMk    = pipeline.ingest_total ?? 0;
    const exec       = (pipeline.exec_mode  || 'DRY_RUN').toUpperCase();
    const killswitch = (pipeline.killswitch || 'off').toLowerCase();
    const latency    = pipeline.latency_ms;

    const unhealthy = !loading && (
      bot !== 'running' ||
      ws !== 'live' ||
      liveMk === 0 ||
      killswitch === 'on'
    );

    const rootStyle = {
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 12px',
      background: 'var(--bg-1)',
      borderBottom: '1px solid var(--bd-1)',
      flexShrink: 0,
      position: 'relative',
      overflow: 'hidden',
    };

    return (
      React.createElement('div', { className: 'portfolio-pipeline-status', style: rootStyle },
        unhealthy && React.createElement('div', {
          style: {
            position: 'absolute', inset: 0,
            background: 'var(--accent-pink-bg)',
            animation: 'ui-pulse 2.4s ease-in-out infinite',
            pointerEvents: 'none',
          },
        }),
        // Left cluster — pills
        React.createElement('div', { style: { display: 'flex', gap: 6, alignItems: 'center', position: 'relative' } },
          React.createElement('span', { className: 'label', style: { color: 'var(--fg-2)' } }, 'PIPELINE'),
          React.createElement(Pill, { text: 'BOT ' + bot.toUpperCase(),
            variant: loading ? 'muted' : variantFor.bot(bot) }),
          React.createElement(Pill, { text: 'WS ' + ws.toUpperCase(),
            variant: loading ? 'muted' : variantFor.ws(ws) }),
          React.createElement(Pill, { text: 'INGEST ' + liveMk + '/' + (totalMk || '—'),
            variant: loading ? 'muted' : variantFor.ingest(liveMk, totalMk) }),
          React.createElement(Pill, { text: 'EXEC ' + exec,
            variant: loading ? 'muted' : variantFor.exec(exec) }),
          React.createElement(Pill, { text: 'KILLSWITCH ' + killswitch.toUpperCase(),
            variant: loading ? 'muted' : variantFor.killswitch(killswitch) })
        ),
        // Right cluster — latency + UTC clock
        React.createElement('div', { style: { marginLeft: 'auto', display: 'flex', gap: 12, alignItems: 'center', position: 'relative', fontFamily: 'var(--font-mono)', fontSize: 10 } },
          latency != null && React.createElement('span', { style: { color: 'var(--fg-1)' } },
            'p50 ', React.createElement('span', { style: { color: 'var(--fg-0)' } }, latency + 'ms')
          ),
          React.createElement('span', { style: { color: 'var(--fg-1)', letterSpacing: '0.06em' } }, now)
        )
      )
    );
  };

  window.Portfolio = window.Portfolio || {};
  window.Portfolio.PipelineStatus = PipelineStatus;
})();
