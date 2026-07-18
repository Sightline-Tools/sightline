(function () {
  if (!window.SIGHTLINE_DEMO) return;

  const originalFetch = window.fetch.bind(window);
  const visibilityOverrides = new Map();
  let demoDataPromise = null;

  document.documentElement.classList.add('demo-mode');

  const style = document.createElement('style');
  style.textContent = `
    html.demo-mode .topbar-controls,
    html.demo-mode a[href="/settings"],
    html.demo-mode a[href="/replay"],
    html.demo-mode button[onclick="exportCurrentLog()"],
    html.demo-mode button[onclick="mergeSelected()"],
    html.demo-mode button[onclick="clearAllEncounters()"],
    html.demo-mode button[onclick="manualEnd()"],
    html.demo-mode button[onclick^="deleteEncounter"],
    html.demo-mode details.collapsible {
      display: none !important;
    }
    html.demo-mode .brand-link::after {
      content: "Demo";
      margin-left: 9px;
      padding: 3px 7px;
      color: #b4c9ff;
      border: 1px solid rgba(37, 99, 255, .42);
      border-radius: 999px;
      background: rgba(37, 99, 255, .12);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    html.demo-mode .encounters-page .container > .pane:last-child > .card:first-child {
      display: none !important;
    }
  `;
  document.head.appendChild(style);

  function getDemoData() {
    if (!demoDataPromise) {
      demoDataPromise = originalFetch('/demo-data.json', { cache: 'no-store' }).then((response) => {
        if (!response.ok) throw new Error('Unable to load demo-data.json');
        return response.json();
      });
    }
    return demoDataPromise;
  }

  function jsonResponse(payload, status = 200) {
    return new Response(JSON.stringify(payload), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function textResponse(payload, status = 200) {
    return new Response(payload, {
      status,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function normalizeEncounterId(id) {
    return String(id);
  }

  function overrideKey(encounterId, actorName) {
    return `${normalizeEncounterId(encounterId)}\u0000${actorName}`;
  }

  function applyActorVisibility(detail) {
    const encounterId = detail.encounter.id;
    const patched = clone(detail);
    for (const actor of patched.metrics.actors) {
      const key = overrideKey(encounterId, actor.actor);
      if (visibilityOverrides.has(key)) {
        actor.hidden = visibilityOverrides.get(key);
        actor.shown = !actor.hidden;
      }
    }
    recomputeVisibleTotals(patched.metrics);
    return patched;
  }

  function recomputeVisibleTotals(metrics) {
    const visibleRows = metrics.actors.filter((actor) => !actor.hidden);
    const totals = metrics.totals;
    const sum = (key) => visibleRows.reduce((total, actor) => total + Number(actor[key] || 0), 0);
    const hits = sum('hits');
    const misses = sum('misses');
    const duration = Math.max(Number(metrics.duration_seconds || 0), 1);
    const healingByAbility = {};

    for (const actor of visibleRows) {
      for (const [abilityName, amount] of Object.entries(actor.healing_by_ability || {})) {
        healingByAbility[abilityName] = (healingByAbility[abilityName] || 0) + Number(amount || 0);
      }
    }

    totals.damage_done = sum('damage_done');
    totals.dps = Number((totals.damage_done / duration).toFixed(2));
    totals.damage_taken = sum('damage_taken');
    totals.damage_absorbed = sum('damage_absorbed');
    totals.damage_blocked = sum('damage_blocked');
    totals.healing_done = sum('healing_done');
    totals.healing_received = sum('healing_received');
    totals.hit_attempts = hits + misses;
    totals.hits = hits;
    totals.misses = misses;
    totals.hit_pct = Number(((hits / Math.max(hits + misses, 1)) * 100).toFixed(2));
    totals.healing_by_ability = healingByAbility;
  }

  async function readJsonBody(init, request) {
    const body = init && Object.prototype.hasOwnProperty.call(init, 'body')
      ? init.body
      : null;
    if (typeof body === 'string' && body.trim()) return JSON.parse(body);
    if (request) {
      const text = await request.clone().text();
      if (text.trim()) return JSON.parse(text);
    }
    return {};
  }

  function exportLogLines(data) {
    return (data.current_session.log_lines || [])
      .map((line) => `[${line.timestamp}] ${line.raw_text}`)
      .join('\n') + '\n';
  }

  async function handleApi(url, method, init, request) {
    const data = await getDemoData();
    const path = url.pathname;

    if (method === 'GET' && path === '/api/status') return jsonResponse(clone(data.status));
    if (method === 'GET' && path === '/api/app-info') return jsonResponse({
      name: 'Sightline', version: 'demo', build_commit: 'demo', build_commit_url: '#',
      source_url: 'https://github.com/Sightline-Tools/sightline', debug_mode: false,
    });
    if (method === 'GET' && path === '/api/settings') return jsonResponse(clone(data.settings));
    if (method === 'PUT' && path === '/api/settings') return jsonResponse(clone(data.settings));

    if (path === '/api/parser/start' || path === '/api/parser/stop') {
      return jsonResponse({ status: 'demo' });
    }

    if (method === 'GET' && path === '/api/parser/sessions/current') {
      return jsonResponse(clone(data.current_session));
    }
    if (method === 'GET' && path === '/api/parser/sessions/current/summary') {
      return jsonResponse(clone(data.current_session_summary));
    }
    if (method === 'GET' && path === '/api/parser/sessions/current/export') {
      return textResponse(exportLogLines(data));
    }

    if (method === 'GET' && path === '/api/encounters') {
      return jsonResponse(clone(data.encounters));
    }

    const detailMatch = path.match(/^\/api\/encounters\/(\d+)$/);
    if (method === 'GET' && detailMatch) {
      const detail = data.encounter_details[detailMatch[1]];
      return detail ? jsonResponse(applyActorVisibility(detail)) : jsonResponse({ detail: 'Encounter not found' }, 404);
    }

    const visibilityMatch = path.match(/^\/api\/encounters\/(\d+)\/actor-visibility$/);
    if (method === 'POST' && visibilityMatch) {
      const payload = await readJsonBody(init || {}, request);
      visibilityOverrides.set(
        overrideKey(visibilityMatch[1], payload.actor_name),
        Boolean(payload.hidden)
      );
      return jsonResponse({ status: 'ok' });
    }

    if (path === '/api/encounters/end-active') return jsonResponse({ status: 'demo' });
    if (path === '/api/encounters/merge') return jsonResponse({ status: 'demo' });
    if (path === '/api/encounters/clear') return jsonResponse({ status: 'demo' });
    if (/^\/api\/encounters\/\d+\/delete$/.test(path)) return jsonResponse({ status: 'demo' });

    return jsonResponse({ detail: `Demo endpoint not implemented: ${method} ${path}` }, 404);
  }

  window.fetch = async function demoFetch(input, init) {
    const request = input instanceof Request ? input : null;
    const url = new URL(request ? request.url : String(input), window.location.href);
    const method = String((init && init.method) || (request && request.method) || 'GET').toUpperCase();

    if (url.origin === window.location.origin && url.pathname.startsWith('/api/')) {
      return handleApi(url, method, init || {}, request);
    }

    return originalFetch(input, init);
  };
})();
