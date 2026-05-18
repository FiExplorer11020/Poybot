// static/dashboard/walletgraph_v3/BackgroundAmbient.jsx
// Polymarket V3 — Wallet Graph "univers" background
// Ambient stars + nebula. Pure canvas, no deps. Respects reduced-motion.

(function() {
  const NUM_STARS = 150;
  const NUM_PLANETS = 6;

  function generateStars(width, height) {
    const stars = [];
    for (let i = 0; i < NUM_STARS; i++) {
      stars.push({
        x: Math.random() * width,
        y: Math.random() * height,
        r: Math.random() * 1.5 + 0.3,            // 0.3-1.8px
        baseOpacity: 0.2 + Math.random() * 0.6,  // 0.2-0.8
        twinkleSpeed: 0.0005 + Math.random() * 0.002,
        twinklePhase: Math.random() * Math.PI * 2,
        // ~30% des étoiles tirent vers le bleu/violet doux (#94a8d6), le reste blanc pur
        tint: Math.random() < 0.3 ? '148, 168, 214' : '255, 255, 255',
      });
    }
    return stars;
  }

  function generatePlanets(width, height) {
    // 4-8 "planètes" éparpillées, plus grosses (4-9px), couleurs subtle violet/blue
    const planets = [];
    for (let i = 0; i < NUM_PLANETS; i++) {
      planets.push({
        x: Math.random() * width,
        y: Math.random() * height,
        r: 4 + Math.random() * 5,  // 4-9px
        color: i % 2 === 0
          ? 'rgba(167, 139, 250, 0.12)'  // violet subtle
          : 'rgba(96, 165, 250, 0.10)',  // blue subtle
      });
    }
    return planets;
  }

  function BackgroundAmbient({ width = '100%', height = '100%' }) {
    const canvasRef = React.useRef(null);
    const animRef = React.useRef(null);
    const starsRef = React.useRef([]);
    const planetsRef = React.useRef([]);

    const reducedMotion = React.useMemo(() => {
      if (typeof window === 'undefined') return false;
      const m = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
      return !!(m && m.matches);
    }, []);

    React.useEffect(() => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;

      function resize() {
        const rect = canvas.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        // Reset transform avant scale pour éviter cumul sur resize multiples
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
        starsRef.current = generateStars(rect.width, rect.height);
        planetsRef.current = generatePlanets(rect.width, rect.height);
      }

      resize();
      const ro = new ResizeObserver(resize);
      ro.observe(canvas);

      function paint(time) {
        const w = canvas.width / dpr;
        const h = canvas.height / dpr;
        if (w === 0 || h === 0) {
          if (!reducedMotion) animRef.current = requestAnimationFrame(paint);
          return;
        }
        ctx.clearRect(0, 0, w, h);

        // 1. Background gradient (radial) — deep space center → violet edges
        const grad = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) / 1.2);
        grad.addColorStop(0, '#06080f');
        grad.addColorStop(1, '#0a0e1a');
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, w, h);

        // 1b. Nebula subtle bottom-right (violet glow)
        const nebula = ctx.createRadialGradient(w * 0.85, h * 0.85, 0, w * 0.85, h * 0.85, Math.max(w, h) * 0.5);
        nebula.addColorStop(0, 'rgba(167, 139, 250, 0.05)');
        nebula.addColorStop(1, 'rgba(167, 139, 250, 0)');
        ctx.fillStyle = nebula;
        ctx.fillRect(0, 0, w, h);

        // 2. Planets (subtle radial gradients, decorative)
        for (const p of planetsRef.current) {
          const pg = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 3);
          pg.addColorStop(0, p.color);
          pg.addColorStop(1, 'transparent');
          ctx.fillStyle = pg;
          ctx.fillRect(p.x - p.r * 3, p.y - p.r * 3, p.r * 6, p.r * 6);
        }

        // 3. Stars (with twinkle if motion allowed)
        for (const s of starsRef.current) {
          const op = reducedMotion
            ? s.baseOpacity
            : s.baseOpacity * (0.7 + 0.3 * Math.sin(time * s.twinkleSpeed + s.twinklePhase));
          ctx.fillStyle = `rgba(${s.tint}, ${op})`;
          ctx.beginPath();
          ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
          ctx.fill();
        }

        if (!reducedMotion) {
          animRef.current = requestAnimationFrame(paint);
        }
      }

      animRef.current = requestAnimationFrame(paint);

      return () => {
        if (animRef.current) cancelAnimationFrame(animRef.current);
        ro.disconnect();
      };
    }, [reducedMotion]);

    return (
      <canvas
        ref={canvasRef}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width,
          height,
          pointerEvents: 'none',  // canvas is purely decorative, never blocks graph clicks
          zIndex: 0,
        }}
      />
    );
  }

  // Export to window so Graph.jsx can compose it
  if (typeof window !== 'undefined') {
    window.WG_V3 = window.WG_V3 || {};
    window.WG_V3.BackgroundAmbient = BackgroundAmbient;
  }
})();
