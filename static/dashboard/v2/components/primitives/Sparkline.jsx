// Sparkline.jsx — inline mini-chart, pure SVG, no library
// Renders a smooth polyline of the given values. No axes, no labels.
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { Sparkline } = window.UI;
//   <Sparkline values={[10, 12, 9, 14, 16, 13, 17]} color="var(--accent-green)" />

/**
 * <Sparkline>
 * @param {number[]} props.values    - Required. Numeric data points. Length >= 2.
 *                                      Single-point or empty arrays render an em-dash.
 * @param {number}   [props.width]   - Pixel width when fluid=false. Default 80.
 * @param {number}   [props.height]  - Pixel height. Default 24.
 * @param {string}   [props.color]   - Stroke / fill color. Default 'var(--accent-blue)'.
 * @param {number}   [props.strokeWidth] - Stroke thickness. Default 1.4.
 * @param {boolean}  [props.fluid]   - If true, fills the parent width
 *                                      (uses preserveAspectRatio='none').
 * @param {number}   [props.fillOpacity] - Area fill opacity below the line. Default 0.12.
 * @param {boolean}  [props.showLast] - Draw a small dot at the last point. Default true.
 * @param {Object}   [props.style]
 *
 * Example:
 *   <Sparkline values={equity} width={120} color="var(--accent-green)" />
 *   <Sparkline values={drawdown} fluid color="var(--accent-red)" />
 */
(function () {
  const Sparkline = ({
    values,
    width = 80,
    height = 24,
    color = 'var(--accent-blue)',
    strokeWidth = 1.4,
    fluid = false,
    fillOpacity = 0.12,
    showLast = true,
    style,
  }) => {
    if (!Array.isArray(values) || values.length < 2) {
      return React.createElement('div', {
        style: {
          width: fluid ? '100%' : width,
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--fg-2)',
          fontSize: 9,
          fontFamily: 'var(--font-mono)',
          ...style,
        },
      }, '—');
    }

    const W = width;
    const H = height;
    const max = Math.max.apply(null, values);
    const min = Math.min.apply(null, values);
    const range = (max - min) || 1;
    const stepX = W / (values.length - 1);

    const pts = values.map((v, i) => {
      const x = i * stepX;
      const y = H - ((v - min) / range) * H;
      return x.toFixed(2) + ',' + y.toFixed(2);
    });

    const lastY = H - ((values[values.length - 1] - min) / range) * H;
    const areaPts = '0,' + H + ' ' + pts.join(' ') + ' ' + W + ',' + H;

    return React.createElement('svg', {
      width: fluid ? '100%' : W,
      height: H,
      viewBox: '0 0 ' + W + ' ' + H,
      preserveAspectRatio: fluid ? 'none' : 'xMidYMid meet',
      style: { display: 'block', ...style },
    },
      React.createElement('polygon', {
        points: areaPts,
        fill: color,
        opacity: fillOpacity,
      }),
      React.createElement('polyline', {
        fill: 'none',
        stroke: color,
        strokeWidth: strokeWidth,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        vectorEffect: fluid ? 'non-scaling-stroke' : 'none',
        points: pts.join(' '),
      }),
      showLast && React.createElement('circle', {
        cx: W,
        cy: lastY,
        r: fluid ? 2.2 : 1.8,
        fill: color,
      })
    );
  };

  window.UI = window.UI || {};
  window.UI.Sparkline = Sparkline;
})();
