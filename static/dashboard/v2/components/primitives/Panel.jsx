// Panel.jsx — generic panel wrapper for the v2 dashboard
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { Panel } = window.UI;
//   <Panel title="Equity" subtitle="USD" status={<Pill text="LIVE" variant="success"/>}>
//     ...
//   </Panel>

/**
 * <Panel>
 * @param {string}     props.title     - Required. Uppercased panel title.
 * @param {string}     [props.subtitle] - Small dim text next to title.
 * @param {ReactNode}  [props.status]   - Right-aligned slot (pills, controls).
 * @param {ReactNode}  props.children   - Body content.
 * @param {boolean}    [props.dense]    - Reduces body padding from 12px to 8px.
 * @param {string}     [props.accent]   - 'green'|'red'|'amber'|'blue'|'violet'|'pink'
 *                                        Adds 2px top accent border.
 * @param {Object}     [props.style]    - Inline style override on root.
 * @param {string}     [props.className] - Extra className on root.
 *
 * Example:
 *   <Panel title="Trades" subtitle="last 200" accent="blue" dense
 *          status={<Pill text="LIVE" variant="success"/>}>
 *     <TradeList rows={trades} />
 *   </Panel>
 */
(function () {
  const Panel = ({
    title,
    subtitle,
    status,
    children,
    dense = false,
    accent,
    style,
    className,
  }) => {
    const cls = ['panel'];
    if (accent) cls.push('panel--accent-' + accent);
    if (className) cls.push(className);

    return (
      React.createElement('div', { className: cls.join(' '), style },
        (title || status || subtitle) &&
          React.createElement('header', { className: 'panel__header' },
            React.createElement('div', { className: 'panel__title-group' },
              title && React.createElement('span', { className: 'panel__title' }, title),
              subtitle && React.createElement('span', { className: 'panel__subtitle' }, subtitle)
            ),
            status && React.createElement('div', { className: 'panel__status' }, status)
          ),
        React.createElement('div',
          { className: 'panel__body' + (dense ? ' panel__body--dense' : '') },
          children
        )
      )
    );
  };

  window.UI = window.UI || {};
  window.UI.Panel = Panel;
})();
