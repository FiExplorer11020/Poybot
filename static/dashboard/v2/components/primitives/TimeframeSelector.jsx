// TimeframeSelector.jsx — segmented control for chart timeframes
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { TimeframeSelector } = window.UI;
//   const [tf, setTf] = React.useState('1h');
//   <TimeframeSelector value={tf} onChange={setTf} />

/**
 * <TimeframeSelector>
 * @param {string}        props.value      - Required. Currently active option.
 * @param {Function}      props.onChange   - Required. (newValue: string) => void.
 * @param {string[]}      [props.options]  - Timeframes to show.
 *                                            Default: ['1m','5m','15m','1h','4h','1d','1w']
 * @param {string}        [props.size]     - 'sm' (compact) | 'md' (default).
 *                                            Currently only affects min-width / padding.
 * @param {Object}        [props.style]
 * @param {string}        [props.className]
 *
 * Example:
 *   const [tf, setTf] = React.useState('1h');
 *   <TimeframeSelector value={tf} onChange={setTf} />
 *
 *   // Restricted set
 *   <TimeframeSelector value={tf} onChange={setTf}
 *                      options={['1h','4h','1d']} />
 */
(function () {
  const DEFAULT_OPTIONS = ['1m', '5m', '15m', '1h', '4h', '1d', '1w'];

  const TimeframeSelector = ({
    value,
    onChange,
    options = DEFAULT_OPTIONS,
    size = 'md',
    style,
    className,
  }) => {
    const cls = ['tf-selector'];
    if (className) cls.push(className);

    const btnSize = size === 'sm'
      ? { padding: '3px 7px', minWidth: 26, fontSize: 9 }
      : null;

    return React.createElement('div',
      { className: cls.join(' '), style, role: 'tablist' },
      options.map((opt) => {
        const active = opt === value;
        const btnCls = ['tf-selector__btn'];
        if (active) btnCls.push('tf-selector__btn--active');
        return React.createElement('button', {
          key: opt,
          type: 'button',
          role: 'tab',
          'aria-selected': active,
          className: btnCls.join(' '),
          style: btnSize,
          onClick: () => { if (!active && typeof onChange === 'function') onChange(opt); },
        }, opt);
      })
    );
  };

  window.UI = window.UI || {};
  window.UI.TimeframeSelector = TimeframeSelector;
})();
