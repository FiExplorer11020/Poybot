// StatusPill.jsx — animated pulse pill for live status indicators
// (BOT RUNNING/STOPPED, WS LIVE/DEAD, INGEST 58/116583, etc.)
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { StatusPill } = window.UI;
//   <StatusPill text="BOT RUNNING" variant="success" />
//   <StatusPill text="WS DEAD"     variant="danger"  pulse={false} />

/**
 * <StatusPill>
 * @param {string}  props.text       - Required. The status label.
 * @param {string}  [props.variant]  - 'success'|'warning'|'danger'|'info'|'violet'|'muted'
 * @param {boolean} [props.pulse]    - Whether the dot should pulse. Default true.
 * @param {string}  [props.detail]   - Optional dim secondary text appended after the
 *                                     status ("INGEST 58/116583" → text='INGEST',
 *                                     detail='58/116583').
 * @param {Object}  [props.style]
 * @param {string}  [props.className]
 *
 * Example:
 *   <StatusPill text="BOT RUNNING" variant="success" />
 *   <StatusPill text="INGEST" detail="58 / 116 583" variant="info" />
 *   <StatusPill text="KILLSWITCH" variant="danger" pulse />
 */
(function () {
  const StatusPill = ({
    text,
    variant = 'muted',
    pulse = true,
    detail,
    style,
    className,
  }) => {
    const cls = ['pill', 'pill-' + variant];
    if (className) cls.push(className);

    const dotCls = ['pill__dot'];
    if (pulse) dotCls.push('pill__dot--pulse');

    return (
      React.createElement('span', { className: cls.join(' '), style },
        React.createElement('span', { className: dotCls.join(' ') }),
        React.createElement('span', null, text),
        detail && React.createElement('span',
          { style: { opacity: 0.6, marginLeft: 4, fontWeight: 400 } },
          detail
        )
      )
    );
  };

  window.UI = window.UI || {};
  window.UI.StatusPill = StatusPill;
})();
