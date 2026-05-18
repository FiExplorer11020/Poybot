// Stat.jsx — small "label + value" display primitive
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { Stat } = window.UI;
//   <Stat label="P&L" value="+1 245.30" suffix="USD" sign="+" />

/**
 * <Stat>
 * @param {string}     props.label     - Required. Uppercased label.
 * @param {*}          props.value     - Required. The numeric/string value.
 * @param {string}     [props.suffix]  - Optional unit suffix (e.g. "USD", "%").
 * @param {string}     [props.sign]    - '+' → green, '-' → red, anything else → neutral.
 *                                        Pass explicitly only when you want forced color;
 *                                        otherwise leave undefined and use the raw value.
 * @param {string}     [props.size]    - 'sm'|'md'|'lg'|'xl' (default 'md').
 * @param {boolean}    [props.row]     - If true, lays out as a horizontal row
 *                                        (label left, value right). Default: column.
 * @param {ReactNode}  [props.sparkline] - Optional <Sparkline> inline after the value.
 * @param {Object}     [props.style]
 * @param {string}     [props.className]
 *
 * Example:
 *   <Stat label="Daily P&L" value="+1 245.30" suffix="USD" sign="+" size="lg" />
 *   <Stat label="Latency p50" value="42" suffix="ms" row />
 *   <Stat label="Equity" value="$46 822" sparkline={<Sparkline values={eq}/>} />
 */
(function () {
  const Stat = ({
    label,
    value,
    suffix,
    sign,
    size = 'md',
    row = false,
    sparkline,
    style,
    className,
  }) => {
    const rootCls = ['stat'];
    if (row) rootCls.push('stat--row');
    if (className) rootCls.push(className);

    const valueCls = ['stat__value', 'stat__value--' + size];
    if (sign === '+') valueCls.push('stat__value--pos');
    else if (sign === '-') valueCls.push('stat__value--neg');
    else valueCls.push('stat__value--neutral');

    return (
      React.createElement('div', { className: rootCls.join(' '), style },
        React.createElement('span', { className: 'stat__label' }, label),
        React.createElement('span', { className: valueCls.join(' ') },
          value,
          suffix && React.createElement('span', { className: 'stat__suffix' }, ' ' + suffix)
        ),
        sparkline && React.createElement('div',
          { style: { marginTop: 4 } }, sparkline
        )
      )
    );
  };

  window.UI = window.UI || {};
  window.UI.Stat = Stat;
})();
