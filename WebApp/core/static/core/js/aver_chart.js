// AVer authorship score chart.
// Reads authorship.report()'s dict from a JSON <script id="aver-data"> tag
// and renders a Chart.js line chart with two Poisson-binomial PMFs
// (author / non-author), the user's score marked, and interactive controls
// (log/linear y-axis, reflect-prior toggle, prior slider). All math is
// server-side: the PMFs and the posterior + verdict for every slider
// position arrive precomputed — this script only draws.

(function () {
  const dataEl = document.getElementById("aver-data");
  if (!dataEl) return;

  const payload = JSON.parse(dataEl.textContent);
  const score = payload.score;
  const n = payload.n;
  const VERDICT_META = {
    "strong-author": ["verdictStrongAuthor", "Strong evidence of authorship"],
    "probable-author": ["verdictProbableAuthor", "Probable author"],
    "inconclusive": ["verdictInconclusive", "Inconclusive"],
    "probable-non-author": ["verdictProbableNonAuthor", "Probable non-author"],
    "strong-non-author": [
      "verdictStrongNonAuthor",
      "Strong evidence of non-authorship",
    ],
  };

  // ── DOM ──────────────────────────────────────────────────────────────────
  const canvas = document.getElementById("aver-dist-chart");
  const headlineEl = document.getElementById("aver-headline-pct");
  const verdictEl = document.getElementById("aver-verdict");
  const togglePrior = document.getElementById("aver-tg-prior");
  const toggleScale = document.getElementById("aver-tg-scale");
  const priorSlider = document.getElementById("aver-prior-slider");
  const priorOut = document.getElementById("aver-prior-out");

  if (!canvas || n === 0) return;

  // The raw PMF curves depend only on the (fixed) items — precomputed
  // server-side, prior-independent.
  const rawA = payload.pmf_author;
  const rawN = payload.pmf_non_author;

  function setToggle(group, btn) {
    group.querySelectorAll("button").forEach((b) => b.classList.remove("is-active"));
    btn.classList.add("is-active");
    update();
  }

  function toggleVal(group) {
    const active = group.querySelector("button.is-active");
    return active ? active.dataset.val : null;
  }

  togglePrior.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => setToggle(togglePrior, btn));
  });
  toggleScale.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => setToggle(toggleScale, btn));
  });
  priorSlider.addEventListener("input", update);

  // ── Chart ────────────────────────────────────────────────────────────────
  const I18N = (window.AVER_I18N) || {};
  const t = (key, fallback) => I18N[key] || fallback;

  // Match the app's body font so chart text doesn't look like a foreign
  // graphic — Chart.js's default Helvetica/Arial renders softer than Inter
  // and visually clashes with the surrounding UI.
  const CHART_FONT_FAMILY =
    "'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif";
  if (window.Chart && Chart.defaults && Chart.defaults.font) {
    Chart.defaults.font.family = CHART_FONT_FAMILY;
  }

  // Chart.js can't read CSS variables — it needs concrete color strings.
  // Resolve them from `:root` at build time so text/grid/border track the
  // active theme and stay legible in dark mode.
  function readThemeColors() {
    const cs = getComputedStyle(document.documentElement);
    const text = cs.getPropertyValue("--text").trim() || "#1f1d18";
    const border = cs.getPropertyValue("--border").trim() || "#d8d2c4";
    const neutral = cs.getPropertyValue("--neutral").trim() || "#8c8c8c";
    return { text, border, neutral };
  }

  function applyThemeToChartDefaults() {
    if (!window.Chart || !Chart.defaults) return;
    const { text, border } = readThemeColors();
    Chart.defaults.color = text;
    Chart.defaults.borderColor = border;
    if (Chart.defaults.scale && Chart.defaults.scale.grid) {
      Chart.defaults.scale.grid.color = border;
    }
  }
  applyThemeToChartDefaults();

  const COLOR_A = "#10b981";
  const COLOR_N = "#f43f5e";
  const COLOR_SCORE = "#7954e0";

  let chart = null;

  // The "Your score" pill is drawn inside the canvas, between the legend
  // and the plotting region. We reserve that vertical band by inflating
  // the legend's `height` in `fit` — Chart.js then pushes the plot area
  // (and therefore `ys.top`) down by exactly that much.
  const SCORE_LABEL_H = 20;
  const SCORE_LABEL_GAP_TOP = 8; // breathing room between legend and pill
  const SCORE_LABEL_GAP_BOTTOM = 4; // breathing room between pill and plot
  const SCORE_LABEL_RESERVED =
    SCORE_LABEL_H + SCORE_LABEL_GAP_TOP + SCORE_LABEL_GAP_BOTTOM;

  const scoreSlotPlugin = {
    id: "scoreSlot",
    beforeInit(ch) {
      const originalFit = ch.legend.fit;
      ch.legend.fit = function () {
        originalFit.call(this);
        this.height += SCORE_LABEL_RESERVED;
      };
    },
  };

  const scoreLinePlugin = {
    id: "scoreLine",
    afterDraw(ch) {
      const xs = ch.scales.x;
      const ys = ch.scales.y;
      const xPos = xs.getPixelForValue(score);
      const c = ch.ctx;
      const label = t("yourScore", "Your score");

      c.save();
      c.font = `600 11px ${CHART_FONT_FAMILY}`;
      const textWidth = c.measureText(label).width;
      const padX = 8;
      const boxW = textWidth + padX * 2;
      const boxH = SCORE_LABEL_H;
      const boxX = Math.min(
        Math.max(xPos - boxW / 2, xs.left),
        xs.right - boxW,
      );
      const boxY = ys.top - boxH - SCORE_LABEL_GAP_BOTTOM;

      c.fillStyle = COLOR_SCORE;
      c.beginPath();
      if (c.roundRect) {
        c.roundRect(boxX, boxY, boxW, boxH, 4);
      } else {
        c.rect(boxX, boxY, boxW, boxH);
      }
      c.fill();

      c.fillStyle = "#ffffff";
      c.textAlign = "center";
      c.textBaseline = "middle";
      c.fillText(label, boxX + boxW / 2, boxY + boxH / 2);

      c.setLineDash([5, 4]);
      c.strokeStyle = COLOR_SCORE;
      c.lineWidth = 2;
      c.beginPath();
      c.moveTo(xPos, ys.top);
      c.lineTo(xPos, ys.bottom);
      c.stroke();
      c.restore();
    },
  };

  function buildChart(dataA, dataN, logScale) {
    const labels = Array.from({ length: n + 1 }, (_, i) => i);
    const peak = Math.max(...dataA, ...dataN);
    const yAxisConfig = logScale
      ? (() => {
          // Snap max up to the next decade above the tallest curve, then
          // show exactly 5 decades below it — a standard log-axis layout
          // for probability charts. Both bounds clamped to a sensible band
          // so the chart never shows visually noisy extremes (1e-9 etc.).
          const decadeMax = Math.min(1, Math.pow(10, Math.ceil(Math.log10(Math.max(peak, 1e-6)))));
          const decadeMin = Math.max(1e-5, decadeMax / 1e5);
          return {
            type: "logarithmic",
            title: { display: true, text: t("yAxisLog", "probability (log)") },
            min: decadeMin,
            max: decadeMax,
            ticks: {
              padding: 6,
              // Label only decade boundaries (powers of 10). On a log axis
              // anything else crowds the major ticks.
              callback(v) {
                const log = Math.log10(v);
                if (Math.abs(log - Math.round(log)) > 0.01) return null;
                const pct = v * 100;
                if (pct >= 1) return pct.toFixed(0) + "%";
                const decimals = Math.max(0, -Math.floor(Math.log10(pct)));
                return pct.toFixed(decimals) + "%";
              },
            },
          };
        })()
      : (() => {
          // Snap max up to the next 2% so ticks land on clean multiples.
          // 30% headroom keeps the score label clear of the tallest curve.
          const step = 0.02;
          const snapMax = Math.min(1, Math.ceil((peak * 1.3) / step) * step);
          return {
            type: "linear",
            beginAtZero: true,
            max: snapMax,
            title: { display: true, text: t("yAxis", "probability") },
            ticks: {
              padding: 6,
              stepSize: step,
              callback: (v) => Math.round(v * 100) + "%",
            },
          };
        })()

    if (chart) chart.destroy();
    chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: t("author", "author"),
            data: dataA,
            borderColor: COLOR_A,
            backgroundColor: "rgba(16, 185, 129, 0.10)",
            borderWidth: 2.5,
            pointRadius: 3,
            pointBackgroundColor: COLOR_A,
            fill: true,
            tension: 0.4,
          },
          {
            label: t("nonAuthor", "non-author"),
            data: dataN,
            borderColor: COLOR_N,
            backgroundColor: "rgba(244, 63, 94, 0.10)",
            borderWidth: 2.5,
            pointRadius: 3,
            pointBackgroundColor: COLOR_N,
            fill: true,
            tension: 0.4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        // Chart.js defaults to window.devicePixelRatio, which is 1 on most
        // non-retina displays — that's where canvas text looks softer than
        // the surrounding HTML. Floor at 2 so every screen gets supersampled
        // rendering (negligible cost at this canvas size).
        devicePixelRatio: Math.max(window.devicePixelRatio || 1, 2),
        layout: { padding: { top: 0 } },
        plugins: {
          legend: {
            display: true,
            position: "top",
            align: "center",
            labels: { padding: 8, boxWidth: 12, boxHeight: 12 },
          },
          tooltip: {
            callbacks: {
              title: (ctx) => t("scoreEq", "score = ") + ctx[0].label,
              label: (c) =>
                c.dataset.label + ": " + (c.parsed.y * 100).toFixed(4) + "%",
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: 0,
            max: n,
            ticks: { stepSize: 1, autoSkip: false },
            title: { display: true, text: t("xAxis", "score (items correct)") },
          },
          y: yAxisConfig,
        },
      },
      plugins: [scoreSlotPlugin, scoreLinePlugin],
    });
  }

  // ── Update ───────────────────────────────────────────────────────────────
  function update() {
    const priorPct = parseInt(priorSlider.value, 10);
    const priorN = priorPct / 100;
    priorOut.textContent = priorSlider.value + "%";
    const priorA = 1 - priorN;

    const reflectPrior = toggleVal(togglePrior) === "yes";
    const logScale = toggleVal(toggleScale) === "log";

    const MIN_VAL = 1e-9;
    // Scaling by the prior is presentation (a multiply), not model math.
    const dataA = rawA.map((v) =>
      Math.max(reflectPrior ? v * priorA : v, MIN_VAL),
    );
    const dataN = rawN.map((v) =>
      Math.max(reflectPrior ? v * priorN : v, MIN_VAL),
    );

    const pA = payload.posteriors_by_prior_pct[priorPct][0];

    if (headlineEl) headlineEl.textContent = (pA * 100).toFixed(1) + "%";
    if (verdictEl) {
      const verdict = payload.verdicts_by_prior_pct[priorPct];
      for (const cls of Object.keys(VERDICT_META)) {
        verdictEl.classList.remove("aver-verdict--" + cls);
      }
      verdictEl.classList.add("aver-verdict--" + verdict);
      const [key, fallback] = VERDICT_META[verdict];
      verdictEl.textContent = t(key, fallback);
    }

    buildChart(dataA, dataN, logScale);
  }

  // The authorship analysis is now the only view, so render immediately.
  update();

  // Re-render when the user toggles dark/light mode — Chart.js bakes the
  // color values in at build time, so we have to rebuild to pick up the
  // new theme. `data-theme` is set on `<html>` by theme-toggle.js.
  const themeObserver = new MutationObserver(() => {
    applyThemeToChartDefaults();
    update();
  });
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
})();
