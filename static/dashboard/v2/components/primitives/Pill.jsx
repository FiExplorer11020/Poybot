// Pill.jsx — small static status/label pill ("PROD", "DRY RUN", "OFFLINE")
// Served via Babel-on-the-fly. Registers on window.UI.
//
// Other JSX files use it like:
//   const { Pill } = window.UI;
//   <Pill text="PROD" variant="success" />

/**
 * <Pill>
 * @param {string} props.text    - Required. The pill label (auto-uppercased by CSS).
 * @param {string} [props.variant] - 'success'|'warning'|'danger'|'info'|'violet'|'muted'
 *                                   Default: 'muted'.
 * @param {Object} [props.style]
 * @param {string} [props.className]
 *
 * Example:
 *   <Pill text="PROD"      variant="success" />
 *   <Pill text="DRY RUN"   variant="warning" />
 *   <Pill text="OFFLINE"   variant="danger"  />
 *   <Pill text="PAPER"     variant="info"    />
 *   <Pill text="IDLE"      variant="muted"   />
 */
(function () {
  const Pill = ({ text, variant = 'muted', style, className }) => {
    const cls = ['pill', 'pill-' + variant];
    if (className) cls.push(className);

    return (
      React.createElement('span', { className: cls.join(' '), style }, text)
    );
  };

  window.UI = window.UI || {};
  window.UI.Pill = Pill;
})();
