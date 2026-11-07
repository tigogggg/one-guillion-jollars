// The member's portfolio card, the same on the portal's 📊 Dashboard and the profile's 📊 Resumo: five tiles (value, gross
// and net gain, today, sell signals or the chosen position's verdict), a bar chart of the money in and the gain or loss
// (days / weeks / months / years, a date range, the money in on or off), and the positions with their company name, the
// portfolio's advice and the % over the chosen period. It reads the profile's API (<base>/data and <base>/history).
//
//   ogjWalletCard({ root, base, prefix, max, tile, onLoad }) -> { reload() }
//     root: the element to fill; base: "/c/<token>"; prefix: the ids' prefix (default "wc"); max: positions listed
//     (default all); tile(k, v, s, c): the page's tile maker; onLoad(summary): called with {value, day, dayP} or null.
//
// Served as its own script (String.raw: no backticks or dollar-brace inside).
export const WALLET_CARD = String.raw`
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmtEur = (x) => x == null || !isFinite(x) ? "—" : new Intl.NumberFormat("pt-PT", { style: "currency", currency: "EUR" }).format(x);
  const fmtNum = (x, d = 2) => x == null || !isFinite(x) ? "—" : new Intl.NumberFormat("pt-PT", { maximumFractionDigits: d, minimumFractionDigits: d }).format(x);
  const fmtPct = (x, d = 1) => x == null || !isFinite(x) ? "—" : (x > 0 ? "+" : "") + fmtNum(x, d) + "%";
  const cls = (x) => x > 0 ? "up" : x < 0 ? "down" : "";
  const store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
                  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };
  const plainTile = (k, v, s = "", c = "") => '<div class="tile"><div class="k">' + k + '</div><div class="v ' + c + '">' + v + '</div>' + (s ? '<div class="s">' + s + '</div>' : "") + '</div>';
  // the company's name, short: "BANCO COMERCIAL PORTUGUES, S.A." -> "Banco Comercial Portugues" (the full one on hover)
  const SUFFIX = /[\s,]+(s\.?\s?a\.?|sgps|n\.?\s?v\.?|se|ag|plc|inc\.?|corp\.?|corporation|ltd\.?|limited|s\.?p\.?a\.?|asa|ab|oyj|co\.?|company|group|holdings?)\.?$/i;
  const nice = (s) => {
    let n = String(s || "").trim(), o;
    do { o = n; n = n.replace(SUFFIX, "").trim(); } while (n !== o && n);
    if (!n) n = String(s || "").trim();
    if (n === n.toUpperCase()) n = n.toLowerCase().replace(/(^|[\s\-&.'(/])(\p{L})/gu, (m, a, b) => a + b.toUpperCase());
    return n;
  };
  const SCALES = [["dias", "Dias", "dias"], ["semanas", "Semanas", "semanas"], ["meses", "Meses", "meses"], ["anos", "Anos", "anos"]];
  const TAX = 0.28;  // Portugal: 28% on the gain (capital gains), only when there is a gain
  const monday = (d) => { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() - (x.getUTCDay() + 6) % 7); return x.toISOString().slice(0, 10); };
  const keyOf = { dias: (d) => d, semanas: monday, meses: (d) => d.slice(0, 7), anos: (d) => d.slice(0, 4) };
  const labelOf = { dias: (k) => k.slice(8, 10) + "/" + k.slice(5, 7), semanas: (k) => k.slice(8, 10) + "/" + k.slice(5, 7), meses: (k) => k.slice(5, 7) + "/" + k.slice(0, 4), anos: (k) => k };
  const PER = { dias: "do dia", semanas: "da semana", meses: "do mês", anos: "do ano" };
  const dmy = (d) => d.split("-").reverse().join("/");

  window.ogjWalletCard = function (o) {
    const root = o.root, base = o.base, P = o.prefix || "wc", max = o.max || Infinity, tile = o.tile || plainTile;
    const $ = (id) => document.getElementById(P + "-" + id);
    const done = (s) => { if (o.onLoad) o.onLoad(s); };
    let redraw = 0, alive = 0;
    window.addEventListener("resize", () => { clearTimeout(redraw); redraw = setTimeout(() => { if (alive) alive(); }, 250); });

    function load() {
      return fetch(base + "/data", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((w) => {
        if (!w || !(w.tx || []).length) { done(null); root.innerHTML = '<p class="muted">Ainda não tens movimentos.</p>'; return; }
        draw(w);
      }).catch(() => { root.innerHTML = '<p class="muted">Não consegui ler a carteira agora.</p>'; });
    }

    function draw(w) {
      let value = 0, cost = 0, day = 0;
      const pos = {};
      for (const t of w.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
        const p = pos[t.symbol] ||= { qty: 0, cost: 0, cur: t.currency, name: t.name || "" };
        if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price + t.fee; } else { const avg = p.qty ? p.cost / p.qty : 0; p.cost -= avg * t.qty; p.qty -= t.qty; }
      }
      const rows = [];
      for (const [sym, p] of Object.entries(pos)) {
        const q = w.quotes[sym], fx = w.fx[p.cur];
        if (!q || fx == null || p.qty <= 1e-9) continue;
        const v = p.qty * q.price * fx, d = p.qty * (q.price - (q.prev ?? q.price)) * fx;
        value += v; cost += p.cost * fx; day += d;
        rows.push({ sym, name: q.name || p.name || "", qty: p.qty, v, c: p.cost * fx, d, pct: q.prev ? (q.price / q.prev - 1) * 100 : null });
      }
      const dayP = value - day ? day / (value - day) * 100 : null;
      done({ value, day, dayP });
      const sells = Object.entries(w.verdicts || {}).filter(([k, v]) => k !== "rules" && v.verdict === "Vender").map(([k]) => k);
      rows.sort((a, b) => b.v - a.v);
      let scale = store.get("dash-wallet-scale", "meses");
      if (!SCALES.some(([k]) => k === scale)) scale = "meses";
      let pick = "";  // "" = the whole portfolio, else a symbol
      let showInv = store.get("dash-wallet-inv", true) !== false;  // off: only the gain or loss, on its own scale
      // each position's advice: the portfolio's verdict (Manter / Reduzir / Vender, rules in the profile), reasons on hover
      const advice = (k) => {
        const v = k && (w.verdicts || {})[k];
        if (!v || !v.verdict || v.verdict === "?") return k ? '<span class="wadv muted small">—</span>' : '<span class="wadv"></span>';
        const icon = { Vender: "🔴", Reduzir: "🟠", Manter: "🟢" }[v.verdict] || "";
        const why = (v.reasons || []).join(" · ") + (v.stop ? " · stop sugerido " + fmtNum(v.stop) : "");
        return '<span class="wadv small ' + (v.verdict === "Vender" ? "down" : v.verdict === "Reduzir" ? "gold" : "up") + '" title="' + esc(why) + '" data-tip="' + esc(why) + '" tabindex="0">' + icon + ' ' + esc(v.verdict) + '</span>';
      };
      const line = (k, label, sub, pct, name) => '<div class="drow wsel' + (k === pick ? ' on' : '') + '" data-k="' + esc(k) + '" data-p="' + (pct ?? "") + '" role="button" tabindex="0"><span class="nm wnm"><span class="wl1"><b>' + label + '</b>' +
        (name ? ' <span class="wname" data-noi18n title="' + esc(name) + '">' + esc(nice(name)) + '</span>' : '') + '</span><span class="wl2 muted small">' + sub + '</span></span>' + advice(k) + '<span class="wp ' + cls(pct) + '">' + fmtPct(pct) + '</span></div>';
      // the tiles follow the chosen row: the whole portfolio or one position
      const tiles = () => {
        const r = pick ? rows.find((x) => x.sym === pick) : { v: value, c: cost, d: day, pct: dayP };
        const verdict = pick ? ((w.verdicts || {})[pick] || {}).verdict : null;
        const gross = r.v - r.c, net = gross > 0 ? gross * (1 - TAX) : gross;
        const pc = (g) => (r.c ? fmtPct(g / r.c * 100, 2) : "—");
        const help = gross > 0 ? "Depois de 28% de imposto sobre o lucro (" + fmtEur(gross * TAX) + "), se vendesses hoje. Em Portugal as mais-valias pagam 28% (taxa liberatória); quem opta pelo englobamento pode pagar outra taxa."
          : "Sem imposto: não há lucro. Em Portugal o imposto de 28% só incide sobre o lucro, quando vendes.";
        return tile("Valor" + (pick ? " · " + esc(pick) : ""), fmtEur(r.v)) +
          tile("Ganho bruto", fmtEur(gross), pc(gross), cls(gross)) +
          '<div class="tile"><div class="k">Ganho líquido <span class="qhelp" tabindex="0" data-tip="' + esc(help) + '" aria-label="' + esc(help) + '">?</span></div><div class="v ' + cls(net) + '">' + fmtEur(net) + '</div><div class="s">' + pc(net) + '</div></div>' +
          tile("Hoje", fmtEur(r.d), fmtPct(r.pct, 2), cls(r.d)) +
          (pick ? tile("Veredito", esc(verdict || "—"), "da carteira para " + esc(pick), verdict === "Vender" ? "down" : verdict ? "up" : "")
            : tile("Sinais de venda", sells.length ? esc(sells.join(", ")) : "nenhum", "veredito da carteira", sells.length ? "down" : "up"));
      };
      const first = w.tx.reduce((m, t) => (t.date < m ? t.date : m), "9999"), todayIso = new Date().toISOString().slice(0, 10);
      let from = first, to = todayIso;
      const id = (n) => P + "-" + n;
      root.innerHTML = '<div class="wtiles" id="' + id("tiles") + '">' + tiles() + '</div>' +
        '<div class="wbar"><div class="wleft"><div class="chips" id="' + id("scale") + '">' + SCALES.map(([k, t]) => '<button class="chip' + (k === scale ? ' on' : '') + '" data-s="' + k + '">' + t + '</button>').join("") + '</div>' +
        '<label class="wchk"><input type="checkbox" id="' + id("inv") + '"' + (showInv ? ' checked' : '') + '> <span>Investimento</span></label></div>' +
        '<div class="wrange"><label>De <input type="date" id="' + id("from") + '" value="' + first + '" min="' + first + '" max="' + todayIso + '"></label>' +
        '<label>a <input type="date" id="' + id("to") + '" value="' + todayIso + '" min="' + first + '" max="' + todayIso + '"></label>' +
        '<button class="chip" id="' + id("all") + '" title="Desde a primeira compra até hoje">Tudo</button></div></div>' +
        '<div id="' + id("chart") + '" class="dash-chart dash-bars"><p class="muted small">A carregar o histórico…</p></div>' +
        '<p class="muted small"><span id="' + id("leg-inv") + '"' + (showInv ? '' : ' hidden') + '><span style="color:#1a56c4">■</span> <span>investido</span> · </span><span style="color:#39ff88">■</span> <span>lucro</span> · <span style="color:#ff4d6d">■</span> <span>prejuízo</span> · <span id="' + id("what") + '">Total</span></p>' +
        '<p class="muted small wper" id="' + id("per") + '">% de hoje</p>' +
        '<div id="' + id("rows") + '">' + line("", "Total", fmtEur(value), dayP) +
        rows.slice(0, max).map((r) => line(r.sym, esc(r.sym), fmtNum(r.qty, r.qty % 1 ? 2 : 0) + ' · ' + fmtEur(r.v), r.pct, r.name)).join("") + '</div>' +
        (rows.length > max ? '<p class="muted small">+ ' + (rows.length - max) + ' posições no perfil</p>' : '');

      // day by day since the first purchase, each position's money in (average cost, in euros at each purchase's rate) and
      // its value at that day's close; the last day is today's live value, the same as the tiles above
      let H = null;
      const build = (h) => {
        const tx = w.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq);
        const first = tx[0].date, today = new Date().toISOString().slice(0, 10);
        const days = [...new Set(Object.values(h.series || {}).flatMap((x) => x.t))].filter((d) => d >= first && d < today).sort();
        days.push(today);
        const cursor = (x) => { let i = 0, v = null; return (d) => { if (!x) return null; while (i < x.t.length && x.t[i] <= d) { if (x.c[i] != null) v = x.c[i]; i++; } return v; }; };
        const rate = (cur, close) => (close ? (cur === "GBp" || cur === "GBX" ? 0.01 : 1) / close : null);
        const fxAt = (cur, d) => { if (!cur || cur === "EUR") return 1; const x = (h.fx || {})[cur]; let v = null; if (x) for (let i = 0; i < x.t.length && x.t[i] <= d; i++) if (x.c[i] != null) v = x.c[i]; return rate(cur, v) ?? w.fx[cur] ?? 1; };
        const px = {}, fxc = {}, st = {}, out = { "": { inv: [], val: [], rg: [] } };
        for (const t of tx) { px[t.symbol] ||= cursor((h.series || {})[t.symbol]); if (t.currency && t.currency !== "EUR") fxc[t.currency] ||= cursor((h.fx || {})[t.currency]); }
        let ti = 0;
        for (const d of days) {
          for (; ti < tx.length && tx[ti].date <= d; ti++) {
            const t = tx[ti], q = st[t.symbol] ||= { qty: 0, inv: 0, rg: 0, cur: t.currency };
            if (t.side === "buy") { q.qty += t.qty; q.inv += (t.qty * t.price + t.fee) * fxAt(t.currency, t.date); }
            else { const avg = q.qty ? q.inv / q.qty : 0; q.rg += (t.qty * t.price - t.fee) * fxAt(t.currency, t.date) - avg * t.qty; q.inv -= avg * t.qty; q.qty -= t.qty; }
          }
          let ti2 = 0, tv = 0, tr = 0;
          for (const [sym, q] of Object.entries(st)) {
            const o = out[sym] ||= { inv: new Array(out[""].inv.length).fill(0), val: new Array(out[""].inv.length).fill(0), rg: new Array(out[""].inv.length).fill(0) };
            const close = px[sym](d), fx = !q.cur || q.cur === "EUR" ? 1 : rate(q.cur, fxc[q.cur](d)) ?? w.fx[q.cur] ?? 1;
            const live = d === today ? rows.find((r) => r.sym === sym) : null;
            const held = q.qty > 1e-9, inv = held ? (live ? live.c : q.inv) : 0;
            const val = held ? (live ? live.v : close != null ? q.qty * close * fx : inv) : 0;
            o.inv.push(inv); o.val.push(val); o.rg.push(q.rg); ti2 += inv; tv += val; tr += q.rg;
          }
          out[""].inv.push(ti2); out[""].val.push(tv); out[""].rg.push(tr);
        }
        return { days, out };
      };
      // each row's % over the chosen period: from the close before it to its last day up to the end date
      const period = () => {
        if (!H || !H.days.length) return;
        let e = -1;
        H.days.forEach((d, i) => { if (d <= to) e = i; });
        if (e < 0) return;
        const k = keyOf[scale](H.days[e]);
        let b = e;
        while (b > 0 && keyOf[scale](H.days[b - 1]) === k) b--;
        const s0 = b - 1;  // the close before the period starts (-1: nothing held yet)
        const live = scale === "dias" && e === H.days.length - 1;  // today: the live change, as in the tile
        if (live) {
          $("rows").querySelectorAll(".wsel").forEach((r) => { const v = r.dataset.p === "" ? null : +r.dataset.p, x = r.querySelector(".wp"); x.className = "wp " + cls(v); x.textContent = fmtPct(v); });
          $("per").textContent = "% de hoje";
          return;
        }
        const pctOf = (o) => {
          if (!o) return null;
          const g = (i) => (i < 0 ? 0 : o.val[i] - o.inv[i] + o.rg[i]), inv0 = s0 < 0 ? 0 : o.inv[s0];
          const base = (s0 < 0 ? 0 : o.val[s0]) + Math.max(0, o.inv[e] - inv0);
          return base ? (g(e) - g(s0)) / base * 100 : null;
        };
        $("rows").querySelectorAll(".wsel").forEach((r) => {
          const v = pctOf(H.out[r.dataset.k]), x = r.querySelector(".wp");
          x.className = "wp " + cls(v); x.textContent = fmtPct(v);
        });
        $("per").textContent = "% " + PER[scale] + " · fecho " + dmy(H.days[Math.max(0, s0)]) + " → " + dmy(H.days[e]);
      };
      // bars: the money in (dark blue), the gain stacked on top (green) or the loss below zero (red), at the end of each period
      const bars = () => {
        if (!$("chart")) return;
        $("what").textContent = pick || "Total";
        if (!window.Plotly || !H) return;
        if (!$("chart").offsetParent) return;  // hidden (another tab): drawn when it shows
        period();
        const o = H.out[pick] || H.out[""], last = {};
        H.days.forEach((d, i) => { if (d >= from && d <= to) last[keyOf[scale](d)] = i; });
        let keys = Object.keys(last).sort().filter((k) => o.inv[last[k]] || o.val[last[k]]);
        const room = Math.max(10, Math.floor(($("chart").clientWidth || 600) / 12));  // at least ~12px per bar
        const n = Math.max(1, Math.ceil(keys.length / room));
        const span = {};  // each bar's first period, when it holds several
        if (n > 1) {
          const kept = [];
          for (let i = keys.length; i > 0; i -= n) { kept.unshift(keys[i - 1]); span[keys[i - 1]] = keys[Math.max(0, i - n)]; }
          keys = kept;
        }
        const unit = SCALES.find(([k]) => k === scale)[2];
        if (keys.length) $("what").textContent = (pick || "Total") + " · " + dmy(H.days[last[span[keys[0]] || keys[0]]]) + " → " +
          dmy(H.days[last[keys[keys.length - 1]]]) + (n > 1 ? " · cada barra junta " + n + " " + unit : "");
        if (!keys.length) { $("chart").innerHTML = '<p class="muted small">Sem histórico nestas datas.</p>'; return; }
        // dd/mm repeats across years: then the year goes in too (Plotly would stack equal labels into one bar)
        const years = new Set(keys.map((k) => k.slice(0, 4))).size > 1 && (scale === "dias" || scale === "semanas");
        const x = keys.map((k) => labelOf[scale](k) + (years ? "/" + k.slice(2, 4) : "")), inv = keys.map((k) => o.inv[last[k]]), val = keys.map((k) => o.val[last[k]]);
        const gain = val.map((v, i) => v - inv[i]);
        const when = keys.map((k) => (span[k] && span[k] !== k ? labelOf[scale](span[k]) + " a " : scale === "semanas" ? "semana de " : "") + labelOf[scale](k) +
          (scale === "dias" ? "" : " (fecho " + H.days[last[k]].slice(8, 10) + "/" + H.days[last[k]].slice(5, 7) + ")"));
        const info = gain.map((g, i) => when[i] + "<br>investido " + fmtEur(inv[i]) + "<br>" + (g >= 0 ? "lucro " : "prejuízo ") + fmtEur(g) + (inv[i] ? " (" + fmtPct((val[i] / inv[i] - 1) * 100, 2) + ")" : "") + "<br>vale " + fmtEur(val[i]));
        $("chart").innerHTML = "";
        Plotly.newPlot(id("chart"), showInv ? [
          { x, y: inv.map((v, i) => (gain[i] < 0 ? val[i] : v)), name: "Investido", type: "bar", marker: { color: "#1a56c4", line: { color: "#3d7bff", width: 1 } }, hovertext: info, hoverinfo: "text" },
          { x, y: gain.map((g) => (g > 0 ? g : 0)), name: "Lucro", type: "bar", marker: { color: "#39ff88" }, hoverinfo: "skip" },
          { x, y: gain.map((g) => (g < 0 ? -g : 0)), name: "Prejuízo", type: "bar", marker: { color: "#ff4d6d" }, hoverinfo: "skip" },
        ] : [
          { x, y: gain, name: "Lucro", type: "bar", marker: { color: gain.map((g) => (g < 0 ? "#ff4d6d" : "#39ff88")) }, hovertext: info, hoverinfo: "text" },
        ], { barmode: "relative", bargap: keys.length < 4 ? 0.6 : 0.25, paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 11 },
          margin: { l: 56, r: 8, t: 8, b: keys.length > 12 ? 56 : 30 }, showlegend: false, hovermode: "x",
          yaxis: { ticksuffix: " €", gridcolor: "#2e2763", zerolinecolor: "#9b96c9" }, xaxis: { type: "category", tickangle: keys.length > 12 ? -45 : 0, nticks: 14 },
          hoverlabel: { bgcolor: "#17123a", bordercolor: "#3d7bff" } },
          { responsive: true, displaylogo: false, displayModeBar: false });
      };
      alive = bars;
      $("scale").addEventListener("click", (e) => {
        const b = e.target.closest("[data-s]");
        if (!b) return;
        scale = b.dataset.s;
        store.set("dash-wallet-scale", scale);
        $("scale").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c.dataset.s === scale));
        bars();
      });
      const choose = (e) => {
        const r = e.target.closest(".wsel");
        if (!r || (e.type === "keydown" && e.key !== "Enter" && e.key !== " ")) return;
        e.preventDefault();
        pick = r.dataset.k;
        $("tiles").innerHTML = tiles();
        $("rows").querySelectorAll(".wsel").forEach((x) => x.classList.toggle("on", x.dataset.k === pick));
        bars();
      };
      const range = () => {
        const f = $("from").value || first, t = $("to").value || todayIso;
        from = f <= t ? f : t; to = f <= t ? t : f;
        bars();
      };
      $("inv").addEventListener("change", (e) => {
        showInv = e.target.checked;
        store.set("dash-wallet-inv", showInv);
        $("leg-inv").hidden = !showInv;
        bars();
      });
      $("from").addEventListener("change", range);
      $("to").addEventListener("change", range);
      $("all").addEventListener("click", () => { $("from").value = first; $("to").value = todayIso; range(); });
      $("rows").addEventListener("click", choose);
      $("rows").addEventListener("keydown", choose);
      fetch(base + "/history", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((h) => {
        H = build(h || { series: {}, fx: {} });
        bars();
      }).catch(() => { H = build({ series: {}, fx: {} }); bars(); });
    }

    load();
    return { reload: load, redraw: () => { if (alive) alive(); } };
  };
})();
`;
