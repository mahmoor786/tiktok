/* Trendpack page. Plain JavaScript, no build step.
   Every piece of text that comes from the web or the model is added with
   textContent (never innerHTML), so nothing it contains can run as code. */
'use strict';

const view = document.getElementById('view');
const tabs = document.getElementById('tabs');
const toastEl = document.getElementById('toast');
let appState = null;      // answer of /api/state
let renderToken = 0;      // changes on every navigation; stops old polling loops

// ---------- tiny helpers ----------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === '') continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch (_) { return null; }
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch (_) { return 'source'; }
}

function when(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const day = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  return `${day}, ${time}`;
}

let toastTimer = 0;
function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2200);
}

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', credentials: 'same-origin', headers: {} };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  let res;
  try { res = await fetch(path, init); }
  catch (_) { throw new Error('The server could not be reached. Check your internet and try again.'); }
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401 && path !== '/api/login') {
    appState = { signed_in: false };
    renderLogin();
    throw new Error('Sign in first.');
  }
  if (!res.ok) {
    throw new Error(typeof data.error === 'string' ? data.error : 'Check what you typed and try again.');
  }
  return data;
}

async function copyText(text, button) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) { ok = false; }
  if (!ok) {
    // Works on plain http:// pages too, where the newer clipboard API is switched off.
    const area = h('textarea', { readonly: true, 'aria-hidden': 'true' });
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    area.setSelectionRange(0, text.length);
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    area.remove();
  }
  if (ok && button) {
    const before = button.textContent;
    button.textContent = 'Copied';
    button.classList.add('done');
    setTimeout(() => { button.textContent = before; button.classList.remove('done'); }, 1600);
  }
  if (!ok) toast('Could not copy. Press and hold the text to copy it.');
}

function copyButton(text, label = 'Copy') {
  const button = h('button', { class: 'copy', type: 'button' }, label);
  button.addEventListener('click', () => copyText(text, button));
  return button;
}

function sourceLinks(ids, sources) {
  const byId = new Map((sources || []).map((s) => [s.id, s]));
  const links = (ids || []).map((id) => byId.get(id)).filter(Boolean).map((s) => {
    const url = safeUrl(s.url);
    const name = hostOf(s.url).includes('vertexaisearch') ? s.title : hostOf(s.url);
    return url ? h('a', { class: 'src', href: url, target: '_blank', rel: 'noopener noreferrer', title: s.title }, name) : null;
  });
  return h('span', { class: 'srcs' }, links);
}

function show(...nodes) {
  view.replaceChildren(...nodes.flat(Infinity).filter(Boolean));
  window.scrollTo(0, 0);
}

function setTab(name) {
  tabs.hidden = !(appState && appState.signed_in);
  for (const link of tabs.querySelectorAll('a')) {
    if (link.dataset.tab === name) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

function busy(button, text) {
  const before = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => { button.disabled = false; button.textContent = before; };
}

function errorNote(message) {
  return h('div', { class: 'note bad', role: 'alert' }, h('p', {}, message));
}

// ---------- sign in ----------

function renderLogin() {
  setTab('');
  const password = h('input', { type: 'password', id: 'pw', autocomplete: 'current-password', required: true });
  const problem = h('div', {});
  const button = h('button', { class: 'btn wide', type: 'submit' }, 'Sign in');
  const form = h('form', { class: 'login', onsubmit: async (event) => {
    event.preventDefault();
    const done = busy(button, 'Signing in');
    try {
      await api('/api/login', { method: 'POST', body: { password: password.value } });
      await start();
    } catch (err) {
      problem.replaceChildren(errorNote(err.message));
      done();
    }
  } },
    h('h1', {}, 'Sign in to your packs'),
    h('p', { class: 'lede' }, 'This page is private. Use the password you were given.'),
    h('div', { class: 'field' }, h('label', { for: 'pw' }, 'Password'), password),
    problem, button);
  show(form);
  password.focus();
}

// ---------- find ----------

let findMode = 'discover';
let pickedCategory = null;

async function renderFind() {
  setTab('find');
  appState = await api('/api/state');
  if (!appState.signed_in) return renderLogin();
  const setup = appState.setup;
  pickedCategory = pickedCategory || appState.default_category;

  const head = [
    h('h1', {}, 'What should the next video sell?'),
    h('p', { class: 'lede' }, `${appState.month} in the UK: ${appState.season.replace(/^[^:]*:\s*/, '')}.`),
  ];
  if (setup.demo) {
    head.push(h('div', { class: 'note' }, h('p', {}, 'Demo mode is on. Everything shown is made-up sample data, not real research.')));
  }
  if (!setup.ready) {
    head.push(h('div', { class: 'note' },
      h('p', {}, 'Two free keys are needed before the research can run: one for Gemini and one for Tavily web search.'),
      h('a', { class: 'btn small', href: '#/settings' }, 'Add keys in Settings')));
  }
  for (const job of appState.active_jobs || []) {
    head.push(h('div', { class: 'note good' },
      h('p', {}, `Still working: ${job.message || 'in progress'} (${job.progress}%)`),
      h('a', { class: 'btn small', href: `#/job/${job.id}` }, 'Watch progress')));
  }

  const body = h('div', {});
  const modes = h('div', { class: 'switch', role: 'group', 'aria-label': 'How to choose the product' },
    h('button', { type: 'button', 'aria-pressed': String(findMode === 'discover'), onclick: () => { findMode = 'discover'; renderFind(); } }, 'Find products'),
    h('button', { type: 'button', 'aria-pressed': String(findMode === 'link'), onclick: () => { findMode = 'link'; renderFind(); } }, 'I have a link'));

  if (findMode === 'discover') {
    const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Category' },
      appState.categories.map((cat) => h('button', {
        class: 'chip', type: 'button', 'aria-pressed': String(cat.id === pickedCategory),
        onclick: (event) => {
          pickedCategory = cat.id;
          for (const chip of chips.children) chip.setAttribute('aria-pressed', String(chip === event.currentTarget));
        } }, cat.label)));
    const note = h('input', { type: 'text', id: 'note', maxlength: 120, placeholder: 'For example: under £20, or gifts for mums' });
    const problem = h('div', {});
    const go = h('button', { class: 'btn wide', type: 'button', disabled: !setup.ready }, 'Find 10 products');
    go.addEventListener('click', async () => {
      const done = busy(go, 'Starting');
      try {
        const started = await api('/api/research', { method: 'POST', body: { category: pickedCategory, note: note.value } });
        location.hash = `#/job/${started.job_id}`;
      } catch (err) { problem.replaceChildren(errorNote(err.message)); done(); }
    });
    body.append(
      h('div', { class: 'label', id: 'cat-label' }, 'Category'), chips,
      h('div', { class: 'field' }, h('label', { for: 'note' }, 'Anything specific?', h('span', { class: 'hint' }, 'Optional')), note),
      problem, go,
      h('p', { class: 'soft small' }, 'Takes about a minute. You then tick the products you want packs for.'));
  } else {
    const url = h('input', { type: 'url', id: 'url', inputmode: 'url', placeholder: 'https://', autocomplete: 'off' });
    const name = h('input', { type: 'text', id: 'pname', maxlength: 120 });
    const problem = h('div', {});
    const go = h('button', { class: 'btn wide', type: 'button', disabled: !setup.ready }, 'Make the pack');
    go.addEventListener('click', async () => {
      const done = busy(go, 'Starting');
      try {
        const started = await api('/api/packs/link', { method: 'POST', body: { url: url.value, name: name.value } });
        location.hash = `#/job/${started.job_id}`;
      } catch (err) { problem.replaceChildren(errorNote(err.message)); done(); }
    });
    body.append(
      h('div', { class: 'field' }, h('label', { for: 'url' }, 'Product link',
        h('span', { class: 'hint' }, 'From TikTok Shop, Amazon or any shop page')), url),
      h('div', { class: 'field' }, h('label', { for: 'pname' }, 'Product name',
        h('span', { class: 'hint' }, 'Optional, but it helps when a shop blocks the link')), name),
      problem, go);
  }

  const recent = h('div', {});
  show(head, modes, body, recent);

  try {
    const { research } = await api('/api/research');
    const ready = research.filter((r) => r.status === 'ready').slice(0, 4);
    if (ready.length) {
      const labels = new Map(appState.categories.map((c) => [c.id, c.label]));
      recent.append(h('h2', {}, 'Recent searches'), h('ul', { class: 'list' }, ready.map((r) =>
        h('li', {}, h('a', { href: `#/research/${r.id}` },
          h('b', {}, `${r.count} products in ${labels.get(r.category) || r.category}`),
          h('small', {}, `${when(r.created_at)}${r.origin === 'auto' ? ', daily search' : ''}`))))));
    }
  } catch (_) { /* the list is optional */ }
}

// ---------- progress ----------

async function renderJob(id) {
  setTab('find');
  const token = renderToken;
  const title = h('h1', {}, 'Working on it');
  const message = h('p', { class: 'lede', 'aria-live': 'polite' }, 'Starting');
  const fill = h('i', {});
  const extra = h('div', {});
  show(h('div', { class: 'working' }, title, h('div', { class: 'meter', role: 'progressbar', 'aria-label': 'Progress' }, fill), message,
    h('p', { class: 'soft small' }, 'You can close this page. The work carries on and will be here when you come back.'), extra));

  while (token === renderToken) {
    let job;
    try { job = await api(`/api/jobs/${id}`); }
    catch (err) { extra.replaceChildren(errorNote(err.message)); return; }
    if (token !== renderToken) return;
    fill.style.width = `${Math.max(job.progress, 4)}%`;
    message.textContent = job.message || 'Working';
    if (job.status === 'done') {
      const ref = job.ref || {};
      if (ref.research_id) location.replace(`#/research/${ref.research_id}`);
      else if (ref.pack_ids && ref.pack_ids.length === 1) location.replace(`#/pack/${ref.pack_ids[0]}`);
      else location.replace('#/packs');
      return;
    }
    if (job.status === 'error') {
      title.textContent = 'That did not work';
      message.textContent = '';
      const ref = job.ref || {};
      extra.replaceChildren(errorNote(job.error || 'Something went wrong.'),
        ref.pack_ids && ref.pack_ids.length === 1
          ? h('a', { class: 'btn', href: `#/pack/${ref.pack_ids[0]}` }, 'Open the pack')
          : h('a', { class: 'btn', href: '#/' }, 'Back to Find'));
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

// ---------- research results ----------

const TICK = () => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke-width', '3');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M5 12.5l4.5 4.5L19 7.5');
  svg.append(path);
  return svg;
};

const MAX_PICKS = 5;

async function renderResearch(id) {
  setTab('find');
  let research;
  try { research = await api(`/api/research/${id}`); }
  catch (err) { return show(h('h1', {}, 'Search not found'), errorNote(err.message), h('a', { class: 'btn', href: '#/' }, 'Back to Find')); }
  if (research.status === 'error') {
    return show(h('h1', {}, 'That search did not work'), errorNote(research.error), h('a', { class: 'btn', href: '#/' }, 'Start a new search'));
  }
  if (research.status !== 'ready') {
    return show(h('h1', {}, 'Still searching'), h('p', { class: 'lede' }, 'This search has not finished yet.'), h('a', { class: 'btn', href: '#/' }, 'Back to Find'));
  }

  const picked = new Set();
  const button = h('button', { class: 'btn wide', type: 'button', disabled: true }, 'Tick the products you want');
  const refresh = () => {
    button.disabled = picked.size === 0;
    button.textContent = picked.size === 0 ? 'Tick the products you want'
      : `Make ${picked.size === 1 ? 'the pack for 1 product' : `packs for ${picked.size} products`}`;
  };
  const confidence = { high: ['strong', 'Strong evidence'], medium: ['', 'Some evidence'], low: ['weak', 'Thin evidence'] };
  const competition = { low: 'Few sellers', medium: 'Some sellers', high: 'Many sellers', unknown: '' };

  const cards = research.products.map((product, index) => {
    const card = h('article', { class: 'product' });
    const tick = h('button', { class: 'tick', type: 'button', 'aria-pressed': 'false', 'aria-label': `Pick ${product.name}` }, TICK());
    tick.addEventListener('click', () => {
      if (!picked.has(index) && picked.size >= MAX_PICKS) return toast(`Up to ${MAX_PICKS} at a time`);
      if (picked.has(index)) picked.delete(index); else picked.add(index);
      card.classList.toggle('picked', picked.has(index));
      tick.setAttribute('aria-pressed', String(picked.has(index)));
      refresh();
    });
    const [tone, toneLabel] = confidence[product.confidence] || confidence.medium;
    const search = `https://www.tiktok.com/search?q=${encodeURIComponent(product.marketplace_search || product.name)}`;
    card.append(
      h('div', { class: 'body' },
        h('h3', {}, product.name),
        h('div', { class: 'meta' },
          product.price_gbp && h('span', { class: 'pill price' }, product.price_gbp),
          h('span', { class: `pill ${tone}` }, toneLabel),
          competition[product.competition] && h('span', { class: 'pill' }, competition[product.competition])),
        product.why_now && h('p', { class: 'why' }, product.why_now)),
      tick,
      h('div', { class: 'body full' },
        h('dl', { class: 'facts' },
          product.evidence && [h('dt', {}, 'What the sources say'), h('dd', {}, product.evidence, ' ', sourceLinks(product.source_ids, research.sources))],
          product.video_angle && [h('dt', {}, 'How to show it'), h('dd', {}, product.video_angle)]),
        h('p', { class: 'small' }, h('a', { href: search, target: '_blank', rel: 'noopener noreferrer' }, 'Look it up on TikTok'))));
    return card;
  });

  button.addEventListener('click', async () => {
    const done = busy(button, 'Starting');
    try {
      const started = await api(`/api/research/${id}/packs`, { method: 'POST', body: { indexes: [...picked] } });
      location.hash = `#/job/${started.job_id}`;
    } catch (err) { toast(err.message); done(); refresh(); }
  });

  show(
    h('h1', {}, `${research.products.length} products in ${research.category_label}`),
    h('p', { class: 'lede' }, `Found ${when(research.created_at)}. Tick up to ${MAX_PICKS} and a full video pack is written for each. Products with no real source behind them were left out.`),
    cards,
    h('div', { class: 'bar' }, h('div', {}, button)));
}

// ---------- pack ----------

function sourcedList(items, sources) {
  return h('ul', { class: 'found' }, items.map((item) => h('li', {}, item.text, ' ', sourceLinks(item.source_ids, sources))));
}

function timecode(seconds) {
  return `0:${String(seconds).padStart(2, '0')}`;
}

async function renderPack(id) {
  setTab('packs');
  let pack;
  try { pack = await api(`/api/packs/${id}`); }
  catch (err) { return show(h('h1', {}, 'Pack not found'), errorNote(err.message), h('a', { class: 'btn', href: '#/packs' }, 'All packs')); }

  const remove = h('button', { class: 'btn danger', type: 'button' }, 'Delete this pack');
  remove.addEventListener('click', async () => {
    if (remove.dataset.sure !== '1') { remove.dataset.sure = '1'; remove.textContent = 'Tap again to delete it for good'; return; }
    try { await api(`/api/packs/${id}`, { method: 'DELETE' }); toast('Pack deleted'); location.hash = '#/packs'; }
    catch (err) { toast(err.message); }
  });
  const retry = async (button, body, label) => {
    const done = busy(button, label);
    try { const started = await api(`/api/packs/${id}/retry`, { method: 'POST', body }); location.hash = `#/job/${started.job_id}`; }
    catch (err) { toast(err.message); done(); }
  };

  if (pack.status === 'queued' || pack.status === 'working') {
    return show(h('h1', {}, pack.product_name), h('p', { class: 'lede' }, 'This pack is being written. It takes a minute or two.'),
      h('button', { class: 'btn quiet', type: 'button', onclick: () => renderPack(id) }, 'Check again'));
  }

  if (pack.status === 'error') {
    const name = h('input', { type: 'text', id: 'rname', maxlength: 120, value: pack.product && pack.product.typed_name || '' });
    const again = h('button', { class: 'btn', type: 'button' }, 'Try again');
    again.addEventListener('click', () => retry(again, { name: name.value }, 'Starting'));
    return show(h('h1', {}, pack.product_name), errorNote(pack.error || 'This pack could not be made.'),
      pack.link_url && h('div', { class: 'field' }, h('label', { for: 'rname' }, 'Product name'), name),
      h('div', { class: 'stack' }, again, remove));
  }

  const body = pack.pack || {};
  const sources = pack.sources || [];
  const nodes = [];

  nodes.push(h('h1', {}, body.product.name));
  nodes.push(h('p', { class: 'lede' }, [body.product.one_line, body.product.price_gbp && `Price seen: ${body.product.price_gbp}.`].filter(Boolean).join(' ')));

  if (pack.status === 'insufficient') {
    const use = h('button', { class: 'btn', type: 'button' }, 'Use it anyway');
    use.addEventListener('click', () => retry(use, { force: true }, 'Starting'));
    nodes.push(h('div', { class: 'note' }, h('h3', {}, 'Not enough real data for this one'), h('p', {}, pack.error)),
      h('div', { class: 'stack' }, use, remove));
    return show(nodes);
  }
  if (body.forced) {
    nodes.push(h('div', { class: 'note' }, h('p', {}, 'Little checked data was found for this product. Compare every detail with the product page before you post.')));
  }

  const single = (body.scenes || []).length === 1;
  const spoken = Boolean(body.voice_in_video);

  // Step 1: Flow
  nodes.push(h('h2', {}, single ? 'Step 1: Make the video in Flow' : 'Step 1: Make the clips in Flow'));
  if (body.reference_image_tip) {
    nodes.push(h('div', { class: 'note good' }, h('h3', {}, 'Add the product photo first'), h('p', {}, body.reference_image_tip)));
  }
  if (single) {
    // One prompt, one clip: the clip is the whole video.
    const scene = body.scenes[0];
    nodes.push(
      h('div', { class: 'block' },
        h('div', { class: 'copyrow' }, h('span', { class: 'label' }, 'Then paste this into Flow'), copyButton(scene.flow_prompt, 'Copy prompt')),
        h('p', { class: 'prompt' }, scene.flow_prompt)),
      h('p', { class: 'soft small' }, `It makes one ${scene.keep_seconds}-second video. That clip is the whole video.`));
    if (spoken) {
      nodes.push(h('div', { class: 'block' }, h('h3', {}, 'The voice is already in the video'),
        h('p', {}, `Flow says: "${scene.voiceover}"`),
        h('p', { class: 'soft small' }, 'Nothing to add. If the clip comes out silent, pick a Flow model that makes sound and generate it again.')));
    } else if (scene.voiceover) {
      nodes.push(h('div', { class: 'extra' },
        h('div', { class: 'line' }, h('div', {}, h('span', {}, 'Voiceover. In TikTok, add it as text and turn on text-to-speech'), scene.voiceover), copyButton(scene.voiceover))));
    }
  } else {
    let at = 0;
    const shots = (body.scenes || []).map((scene) => {
      const from = at;
      at += scene.keep_seconds;
      return h('li', { class: 'shot' },
        h('div', { class: 'time' }, h('b', {}, timecode(from))),
        h('div', { class: 'card' },
          h('p', { class: 'goal' }, `Scene ${scene.n}: ${scene.goal}`),
          h('div', { class: 'block' },
            h('div', { class: 'copyrow' }, h('span', { class: 'label' }, 'Paste into Flow'), copyButton(scene.flow_prompt, 'Copy prompt')),
            h('p', { class: 'prompt' }, scene.flow_prompt)),
          h('div', { class: 'extra' },
            scene.on_screen_text && h('div', { class: 'line' }, h('div', {}, h('span', {}, 'Text on screen'), scene.on_screen_text), copyButton(scene.on_screen_text)),
            scene.voiceover && h('div', { class: 'line' }, h('div', {}, h('span', {}, 'Voiceover'), scene.voiceover), copyButton(scene.voiceover)),
            h('p', { class: 'soft small' }, `Keep the best ${scene.keep_seconds} seconds of this clip.`))));
    });
    nodes.push(h('p', { class: 'soft small' }, `One clip per scene, about ${body.total_seconds} seconds once joined.`), h('ol', { class: 'shots' }, shots));
    if (body.voiceover_full) {
      nodes.push(h('div', { class: 'block' },
        h('div', { class: 'copyrow' }, h('span', { class: 'label' }, 'Full voiceover, for TikTok text-to-speech'), copyButton(body.voiceover_full)),
        h('p', {}, body.voiceover_full)));
    }
  }

  // Step 2: opening text
  if (body.hooks && body.hooks.length) {
    nodes.push(h('h2', {}, 'Step 2: Add the opening text in TikTok'),
      h('p', { class: 'soft small' }, 'This is the big text on the video for the first seconds, to stop people scrolling. In TikTok tap Text and paste one. Post once with A and once with B, then keep whichever gets more views.'),
      h('div', { class: 'hooks' }, body.hooks.map((hook) => h('div', { class: 'block hook' },
        h('span', { class: 'ab', 'aria-hidden': 'true' }, hook.label),
        h('div', { class: 'copyrow' }, h('span', { class: 'label' }, `Opening ${hook.label}`), copyButton(hook.on_screen_text)),
        h('p', { class: 'said' }, hook.on_screen_text),
        !spoken && hook.voiceover && h('p', {}, h('span', { class: 'soft' }, 'Voiceover: '), hook.voiceover),
        hook.why && h('p', { class: 'soft small' }, hook.why)))));
  }

  // Step 3: caption
  nodes.push(h('h2', {}, 'Step 3: Paste the caption'), h('div', { class: 'block' },
    h('div', { class: 'copyrow' }, h('span', { class: 'label' }, 'Caption and hashtags'), copyButton(body.caption_full)),
    h('p', { class: 'prompt' }, body.caption_full)));

  // Step 4: post
  nodes.push(h('h2', {}, 'Step 4: Post it'));
  const posting = [];
  if (body.best_time_uk && body.best_time_uk.window) posting.push(h('p', {}, h('b', {}, `Post ${body.best_time_uk.window} (UK time). `), body.best_time_uk.why));
  if (body.sound) posting.push(h('p', {}, h('b', {}, 'Sound: '), spoken ? 'keep any music quiet so the voice stays clear. ' : '', body.sound));
  if (posting.length) nodes.push(h('div', { class: 'block' }, posting));

  const search = pack.product && pack.product.marketplace_search || body.product.name;
  const checks = [
    `Tag the product. Search the affiliate marketplace for: ${search}`,
    'Turn on the "AI-generated content" label.',
    'Keep #ad in the caption, or turn on "Disclose commercial content".',
    'Check the price and every claim against the product page.',
  ];
  nodes.push(h('ul', { class: 'checks' }, checks.map((text, index) =>
    h('li', {}, h('input', { type: 'checkbox', id: `chk${index}` }), h('label', { for: `chk${index}` }, text)))));

  if (body.claims_to_avoid && body.claims_to_avoid.length) {
    nodes.push(h('h2', {}, 'Do not say'), h('ul', { class: 'found' }, body.claims_to_avoid.map((claim) => h('li', {}, claim))));
  }

  if (body.facts.length || body.buyer_insights.length) {
    nodes.push(h('h2', {}, 'What the research found'),
      h('p', { class: 'soft small' }, 'Everything in the script comes from these. Tap a source to check it.'));
    if (body.facts.length) nodes.push(h('h3', {}, 'Product facts'), sourcedList(body.facts, sources));
    if (body.buyer_insights.length) nodes.push(h('h3', {}, 'What buyers say'), sourcedList(body.buyer_insights, sources));
  }
  if (body.format && body.format.name) {
    nodes.push(h('h3', {}, `Why this video style: ${body.format.name}`),
      h('p', { class: 'soft' }, body.format.why, ' ', sourceLinks(body.format.source_ids, sources)));
  }

  // results
  const views = h('input', { type: 'number', id: 'views', min: 0, inputmode: 'numeric', value: pack.views == null ? '' : pack.views });
  const sales = h('input', { type: 'number', id: 'sales', min: 0, inputmode: 'numeric', value: pack.sales == null ? '' : pack.sales });
  const hookUsed = h('select', { id: 'hook' },
    [['', 'Not sure'], ['A', 'Opening A'], ['B', 'Opening B']].map(([value, label]) =>
      h('option', { value, selected: pack.hook_used === value }, label)));
  const notes = h('input', { type: 'text', id: 'rnotes', maxlength: 300, value: pack.notes || '' });
  const save = h('button', { class: 'btn quiet wide', type: 'button' }, 'Save results');
  save.addEventListener('click', async () => {
    const done = busy(save, 'Saving');
    try {
      await api(`/api/packs/${id}/results`, { method: 'POST', body: {
        posted: true,
        views: views.value === '' ? null : Number(views.value),
        sales: sales.value === '' ? null : Number(sales.value),
        hook_used: hookUsed.value, notes: notes.value } });
      toast('Results saved');
    } catch (err) { toast(err.message); }
    done();
  });
  nodes.push(h('h2', {}, 'How did it do?'),
    h('p', { class: 'soft small' }, 'Fill this in a few days after posting. Future product picks and scripts learn from it.'),
    h('div', { class: 'two' },
      h('div', { class: 'field' }, h('label', { for: 'views' }, 'Views'), views),
      h('div', { class: 'field' }, h('label', { for: 'sales' }, 'Sales'), sales)),
    h('div', { class: 'field' }, h('label', { for: 'hook' }, 'Opening used'), hookUsed),
    h('div', { class: 'field' }, h('label', { for: 'rnotes' }, 'Note', h('span', { class: 'hint' }, 'Optional')), notes),
    save, h('p', {}, remove));

  show(nodes);
}

// ---------- packs list ----------

const STATE_LABEL = { ready: 'Ready', queued: 'Waiting', working: 'Writing', insufficient: 'Thin data', error: 'Needs a retry' };

async function renderPacks() {
  setTab('packs');
  const { packs } = await api('/api/packs');
  if (!packs.length) {
    return show(h('h1', {}, 'Your packs'), h('p', { class: 'lede' }, 'No packs yet. Each pack is everything for one video: Flow prompts, script, caption and hashtags.'),
      h('a', { class: 'btn', href: '#/' }, 'Find a product'));
  }
  show(h('h1', {}, 'Your packs'),
    h('ul', { class: 'list' }, packs.map((pack) => {
      const result = pack.views != null ? `${Number(pack.views).toLocaleString('en-GB')} views, ${pack.sales || 0} sales` : (pack.format || '');
      return h('li', {}, h('a', { href: `#/pack/${pack.id}` },
        h('b', {}, pack.product_name),
        h('span', { class: `state ${pack.status}` }, STATE_LABEL[pack.status] || pack.status),
        h('small', {}, [when(pack.created_at), result].filter(Boolean).join(', '))));
    })));
}

// ---------- settings ----------

async function renderSettings(report) {
  setTab('settings');
  const s = await api('/api/settings');
  const keyField = (id, label, hint, saved) => {
    const input = h('input', { type: 'password', id, autocomplete: 'off', placeholder: saved.saved ? `Saved${saved.ending ? `, ends in ${saved.ending}` : ''}. Paste to replace` : 'Paste the key here' });
    return [h('div', { class: 'field' }, h('label', { for: id }, label, h('span', { class: 'hint' }, hint)), input), input];
  };
  const [geminiField, gemini] = keyField('gk', 'Gemini key', 'Free from aistudio.google.com. It does the thinking and writing.', s.gemini_key);
  const [tavilyField, tavily] = keyField('tk', 'Tavily key', 'Free from tavily.com, 1,000 searches a month. It does the web research.', s.tavily_key);
  const results = h('div', {});
  const check = h('button', { class: 'btn wide', type: 'button' }, 'Save keys and check connections');

  const showReport = (data) => {
    results.replaceChildren(
      h('div', { class: `note ${data.ready ? 'good' : 'bad'}` }, h('p', {}, data.ready
        ? 'Everything needed is connected. You can start finding products.'
        : 'Something still needs fixing. See below.')),
      h('ul', { class: 'checklist' }, data.checks.map((item) =>
        h('li', { class: item.ok ? 'ok' : (item.optional ? 'info' : 'no') }, h('b', {}, item.name), item.detail))));
  };
  check.addEventListener('click', async () => {
    const done = busy(check, 'Checking, about 20 seconds');
    try {
      await api('/api/settings', { method: 'POST', body: { gemini_key: gemini.value, tavily_key: tavily.value } });
      const data = await api('/api/settings/test', { method: 'POST' });
      gemini.value = ''; tavily.value = '';
      showReport(data);
    } catch (err) { results.replaceChildren(errorNote(err.message)); }
    done();
  });
  if (report) showReport(report);

  const clip = h('input', { type: 'number', id: 'clip', min: 4, max: 12, value: s.clip_seconds, inputmode: 'numeric' });
  const scenes = h('input', { type: 'number', id: 'scenes', min: 1, max: 6, value: s.scenes, inputmode: 'numeric' });
  const voiceIn = h('input', { type: 'checkbox', id: 'voicein', checked: s.voice_in_video === '1' });
  const voiceStyle = h('input', { type: 'text', id: 'vstyle', maxlength: 80, value: s.voice_style });
  const auto = h('input', { type: 'checkbox', id: 'auto', checked: s.auto_daily === '1' });
  const autoTime = h('input', { type: 'time', id: 'atime', value: s.auto_time });
  const cats = (appState && appState.categories) || [];
  const autoCat = h('select', { id: 'acat' }, cats.map((c) => h('option', { value: c.id, selected: c.id === s.default_category }, c.label)));
  const notes = h('textarea', { id: 'notes', maxlength: 2000 });
  notes.value = s.account_notes;
  const save = h('button', { class: 'btn wide', type: 'button' }, 'Save settings');
  save.addEventListener('click', async () => {
    const done = busy(save, 'Saving');
    try {
      await api('/api/settings', { method: 'POST', body: {
        clip_seconds: Number(clip.value) || 8, scenes: Number(scenes.value) || 1,
        auto_daily: auto.checked, auto_time: autoTime.value || '07:30',
        default_category: autoCat.value, account_notes: notes.value,
        voice_in_video: voiceIn.checked, voice_style: voiceStyle.value } });
      toast('Settings saved');
    } catch (err) { toast(err.message); }
    done();
  });
  const out = h('button', { class: 'btn danger', type: 'button' }, 'Sign out');
  out.addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); appState = { signed_in: false }; renderLogin(); });

  show(
    h('h1', {}, 'Settings'),
    h('h2', {}, 'Connections'),
    geminiField, tavilyField, check, results,
    h('h2', {}, 'Video'),
    h('div', { class: 'two' },
      h('div', { class: 'field' }, h('label', { for: 'clip' }, 'Seconds per Flow clip'), clip),
      h('div', { class: 'field' }, h('label', { for: 'scenes' }, 'Prompts per video'), scenes)),
    h('p', { class: 'soft small' }, '1 means one prompt and one clip, which is the whole video. A higher number gives a longer video made of several clips that she joins herself.'),
    h('div', { class: 'toggle' }, voiceIn, h('label', { for: 'voicein' }, 'Flow speaks the voiceover in the video', h('span', { class: 'hint' }, 'The prompt includes the spoken line, so there is nothing to add in TikTok. Works with one prompt per video.'))),
    h('div', { class: 'field' }, h('label', { for: 'vstyle' }, 'Voice', h('span', { class: 'hint' }, 'How the narrator should sound')), voiceStyle),
    h('h2', {}, 'Daily search'),
    h('div', { class: 'toggle' }, auto, h('label', { for: 'auto' }, 'Find 10 new products every morning', h('span', { class: 'hint' }, 'They wait under Find until you pick.'))),
    h('div', { class: 'two' },
      h('div', { class: 'field' }, h('label', { for: 'atime' }, 'Time (UK)'), autoTime),
      h('div', { class: 'field' }, h('label', { for: 'acat' }, 'Usual category'), autoCat)),
    h('h2', {}, 'About this account'),
    h('div', { class: 'field' }, h('label', { for: 'notes' }, 'What works and what does not',
      h('span', { class: 'hint' }, 'The research reads this every time. Edit it as you learn.')), notes),
    save,
    h('p', {}, out));
}

// ---------- router ----------

const routes = [
  [/^#\/?$/, () => renderFind()],
  [/^#\/job\/(\d+)$/, (m) => renderJob(m[1])],
  [/^#\/research\/(\d+)$/, (m) => renderResearch(m[1])],
  [/^#\/packs$/, () => renderPacks()],
  [/^#\/pack\/(\d+)$/, (m) => renderPack(m[1])],
  [/^#\/settings$/, () => renderSettings()],
];

async function route() {
  renderToken += 1;
  if (!appState || !appState.signed_in) return renderLogin();
  const hash = location.hash || '#/';
  for (const [pattern, handler] of routes) {
    const match = hash.match(pattern);
    if (match) {
      try { await handler(match); }
      catch (err) { if (appState && appState.signed_in) show(h('h1', {}, 'Something went wrong'), errorNote(err.message), h('a', { class: 'btn', href: '#/' }, 'Back to Find')); }
      return;
    }
  }
  location.hash = '#/';
}

async function start() {
  try { appState = await api('/api/state'); }
  catch (err) { return show(h('h1', {}, 'The server is not answering'), errorNote(err.message)); }
  await route();
}

window.addEventListener('hashchange', route);
start();
