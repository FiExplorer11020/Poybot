// KpiTile.jsx — large headline stat tile ($46 822-style metric block)
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { KpiTile } = window.UI;
//   <KpiTile label="EQUITY" value="$46 822" secondary="peak $48 110" accent="green"
//            trend={{ delta: "+1.24%", sign: "+" }} sparkline={[...]} />

/**
 * <KpiTile>
 * @param {string}   props.label        - Required. Uppercased label (top line).
 * @param {*}        props.value        - Required. Big monospace value.
 * @param {string}   [props.secondary]  - Subtitle line below value (dim).
 * @param {number[]} [props.sparkline]  - Optional values array → renders inline Sparkline.
 * @param {string}   [props.accent]     - 'green'|'red'|'amber'|'blue'|'violet'
 *                                         Adds 2px left accent border.
 * @param {Object}   [props.trend]      - { delta: string, sign: '+'|'-'|'flat' }
 *                                         Small inline up/down trend with color.
 * @param {string}   [props.valueSign]  - '+' green, '-' red — forces value color.
 * @param {Object}   [props.style]
 * @param {string}   [props.className]
 *
 * Example:
 *   <KpiTile label="EQUITY"  value="$46 822" secondary="peak $48 110"
 *            accent="green"  trend={{ delta:'+1.24%', sign:'+' }}
 *            sparkline={equityPoints} />
 *
 *   <KpiTile label="DRAWDOWN" value="-2.69%" secondary="max -8.42%"
 *            accent="red" valueSign="-" />
 */
(function () {
  const Sparkline = () => (window.UI && window.UI.Sparkline) || null;

  const trendColor = (sign) => {
    if (sign === '+') return 'var(--accent-green)';
    if (sign === '-') return 'var(--accent-red)';
    return 'var(--fg-1)';
  };

  const trendGlyph = (sign) => {
    if (sign === '+') return '▲';
    if (sign === '-') return '▼';
    return '·';
  };

  const KpiTile = ({
    label,
    value,
    secondary,
    sparkline,
    accent,
    trend,
    valueSign,
    style,
    className,
  }) => {
    const cls = ['kpi-tile'];
    if (accent) cls.push('kpi-tile--accent-' + accent);
    if (className) cls.push(className);

    const valueCls = ['kpi-tile__value'];
    if (valueSign === '+') valueCls.push('kpi-tile__value--pos');
    else if (valueSign === '-') valueCls.push('kpi-tile__value--neg');

    const Spark = Sparkline();

    return (
      React.createElement('div', { className: cls.join(' '), style },
        React.createElement('div', {
          style: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 },
        },
          React.createElement('span', { className: 'kpi-tile__label' }, label),
          trend && React.createElement('span', {
            className: 'kpi-tile__trend',
            style: { color: trendColor(trend.sign) },
          },
            React.createElement('span', { style: { fontSize: 8 } }, trendGlyph(trend.sign)),
            React.createElement('span', null, trend.delta)
          )
        ),
        React.createElement('div', { className: valueCls.join(' ') }, value),
        secondary && React.createElement('div', { className: 'kpi-tile__secondary' }, secondary),
        sparkline && Array.isArray(sparkline) && Spark &&
          React.createElement('div', { className: 'kpi-tile__sparkline' },
            React.createElement(Spark, {
              values: sparkline,
              fluid: true,
              height: 24,
              color: accent ? 'var(--accent-' + accent + ')' : 'var(--accent-blue)',
              strokeWidth: 1.4,
            })
          )
      )
    );
  };

  window.UI = window.UI || {};
  window.UI.KpiTile = KpiTile;
})();
