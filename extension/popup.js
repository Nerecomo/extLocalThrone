// Popup logic: prefill the root domain of the active tab, talk to the native host.
import { rootDomainFromUrl, cleanEntry } from './lib/rootDomain.js';

const HOST = 'com.throne.site_adder';
const params = new URLSearchParams(location.search);
const inWindow = params.get('mode') === 'window'; // opened as a separate window (needed for the file picker)

const $ = (id) => document.getElementById(id);
const input = $('domain-input');
const addBtn = $('add-btn');
const list = $('list');
const statusBox = $('status');

let entries = [];
let busy = false;

// ---------- native messaging ----------

class HostMissing extends Error {}

async function callHost(message) {
  let reply;
  try {
    reply = await chrome.runtime.sendNativeMessage(HOST, message);
  } catch (e) {
    const text = String(e?.message || e);
    // Chrome/Yandex: "Specified native messaging host not found." / "Access to the specified native messaging host is forbidden."
    // Firefox: "No such native application com.throne.site_adder"
    if (/not found|forbidden|no such native application/i.test(text)) throw new HostMissing(text);
    throw new Error(`Host-приложение не ответило: ${text}`);
  }
  if (!reply) throw new Error('Пустой ответ host-приложения');
  if (!reply.ok) {
    const err = new Error(reply.message || 'Ошибка host-приложения');
    err.code = reply.code;
    throw err;
  }
  return reply;
}

// ---------- rendering ----------

function setStatus(state, text, detail = '') {
  if (!state) {
    statusBox.hidden = true;
    return;
  }
  statusBox.dataset.state = state;
  statusBox.querySelector('.status-text').textContent = text;
  statusBox.querySelector('.status-detail').textContent = detail;
  statusBox.hidden = false;
}

function setBusy(on) {
  busy = on;
  addBtn.disabled = on;
  for (const id of ['export-btn', 'import-btn']) $(id).setAttribute('aria-busy', String(on));
}

function renderList(newOnes = []) {
  const tpl = $('item-template').content.firstElementChild;
  const fresh = new Set(newOnes);
  list.replaceChildren(...entries.map((value) => {
    const li = tpl.cloneNode(true);
    li.querySelector('.item-name').textContent = value;
    li.querySelector('.item-name').title = value;
    li.dataset.value = value;
    if (fresh.has(value)) li.classList.add('item--new');
    return li;
  }));
  $('empty').hidden = entries.length > 0;
  const added = list.querySelector('.item--new');
  if (added) added.scrollIntoView({ block: 'nearest' });
}

function applyReply(reply, newOnes) {
  if (Array.isArray(reply.entries)) entries = reply.entries;
  if (reply.profile) {
    $('profile-name').textContent = reply.profile.name;
    $('profile').hidden = false;
  }
  renderList(newOnes);
}

function showNoHost() {
  $('main').hidden = true;
  $('no-host').hidden = false;
  $('ext-id').textContent = chrome.runtime.id;
}

function showError(e) {
  if (e instanceof HostMissing) return showNoHost();
  if (e.code === 'invalid') {
    input.setAttribute('aria-invalid', 'true');
    return setStatus('config-error', 'Некорректное значение', e.message);
  }
  if (e.code === 'busy') return setStatus('info', 'Подождите', e.message);
  setStatus('config-error', 'Ошибка доступа к конфигу Throne', e.message);
}

/** Status line after a change, by what the host did with the proxy. */
function restartStatus(reply, okText, okDetail = '') {
  const r = reply.restart || {};
  if (r.reason === 'throne-off') {
    return setStatus('throne-off', 'Throne не запущен — изменения применятся при следующем запуске', okText);
  }
  if (r.restarted) {
    return setStatus('success', okText, [okDetail, r.warning || 'Прокси перезапущен'].filter(Boolean).join(' · '));
  }
  if (r.reason === 'core-off' || r.reason === 'not-running') {
    return setStatus('success', okText, [okDetail, 'Прокси сейчас выключен — правило применится при подключении']
      .filter(Boolean).join(' · '));
  }
  if (r.reason === 'unchanged') return setStatus('success', okText, okDetail);
  if (r.reason === 'api-off') {
    return setStatus('throne-off', `${okText}, но ещё не применено`,
      'Включите Throne API: Настройки → Integration Settings → Throne API, затем переподключите профиль');
  }
  if (r.reason === 'manual') return setStatus('info', `${okText} — переподключите профиль в Throne`, okDetail);
  if (r.reason === 'denied') {
    return setStatus('config-error', okText, 'Не удалось перезапустить ядро Throne — нажмите «Restart Proxy» в трее');
  }
  // An API error: the change is saved, only applying it failed.
  return setStatus('config-error', `${okText}, но Throne не применил изменение`,
    r.message || r.reason || 'неизвестная ошибка');
}

// ---------- actions ----------

async function refresh() {
  try {
    const reply = await callHost({ cmd: 'status' });
    $('main').hidden = false;
    $('no-host').hidden = true;
    applyReply(reply);
    if (!reply.throneRunning) {
      setStatus('throne-off', 'Throne не запущен — изменения применятся при следующем запуске');
    } else if (reply.restartMethod === 'api' && reply.api && !reply.api.enabled) {
      setStatus('throne-off', 'Throne API выключен — изменения не применятся автоматически',
        reply.api.supported
          ? 'Включите: Настройки → Integration Settings → Throne API'
          : 'Нужен Throne 1.4.0-beta.2 или новее с включённым Throne API');
    } else {
      setStatus(null);
    }
  } catch (e) {
    showError(e);
  }
}

async function add(event) {
  event.preventDefault();
  if (busy) return;
  input.removeAttribute('aria-invalid');
  let value;
  try {
    value = cleanEntry(input.value);
  } catch (e) {
    input.setAttribute('aria-invalid', 'true');
    return setStatus('config-error', 'Некорректное значение', e.message);
  }
  setBusy(true);
  setStatus('working', 'Применяю…');
  try {
    const reply = await callHost({ cmd: 'add', value });
    applyReply(reply, reply.added);
    if (reply.added?.length) {
      restartStatus(reply, 'Добавлено', reply.added[0]);
    } else if (Object.keys(reply.covered || {}).length) {
      const [[what, by]] = Object.entries(reply.covered);
      setStatus('exists', 'Уже есть в списке', `${what} покрывается записью ${by}`);
    } else {
      setStatus('exists', 'Уже есть в списке', reply.exists?.[0] || value);
    }
  } catch (e) {
    showError(e);
  } finally {
    setBusy(false);
  }
}

async function remove(li) {
  if (busy) return;
  const value = li.dataset.value;
  setBusy(true);
  li.classList.add('item--removing');
  setStatus('working', 'Применяю…');
  try {
    const reply = await callHost({ cmd: 'remove', value });
    await new Promise((r) => setTimeout(r, 200)); // let the row animation finish
    applyReply(reply);
    if (reply.removed?.length) restartStatus(reply, 'Удалено', value);
    else setStatus('info', 'Записи уже нет в списке', value);
  } catch (e) {
    li.classList.remove('item--removing');
    showError(e);
  } finally {
    setBusy(false);
  }
}

function exportList() {
  const stamp = new Date().toISOString().slice(0, 10);
  const text = `# Throne: домены профиля «${$('profile-name').textContent}», ${stamp}\n${entries.join('\n')}\n`;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  a.download = `throne-domains-${stamp}.txt`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
  setStatus('info', 'Экспортировано записей: ' + entries.length, a.download);
}

function parseImport(text) {
  const trimmed = text.trim();
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    const data = JSON.parse(trimmed);
    const arr = Array.isArray(data) ? data : data.entries || data.domains;
    if (!Array.isArray(arr)) throw new Error('В JSON нет массива entries/domains');
    return arr.map(String);
  }
  return trimmed.split(/\r?\n/).map((l) => l.replace(/#.*/, '').trim()).filter(Boolean);
}

async function importFile(file) {
  if (!file || busy) return;
  let values;
  const bad = [];
  try {
    const raw = parseImport(await file.text());
    values = [];
    for (const v of raw) {
      try {
        values.push(cleanEntry(v));
      } catch {
        bad.push(v);
      }
    }
  } catch (e) {
    return setStatus('config-error', 'Не удалось прочитать файл', e.message);
  }
  if (!values.length) return setStatus('config-error', 'В файле нет подходящих записей', bad.slice(0, 3).join(', '));
  setBusy(true);
  setStatus('working', `Импортирую ${values.length}…`);
  try {
    const reply = await callHost({ cmd: 'import', values });
    applyReply(reply, reply.added);
    const skipped = (reply.exists?.length || 0) + Object.keys(reply.covered || {}).length;
    const detail = `пропущено как дубли: ${skipped}` + (bad.length ? `, с ошибкой: ${bad.length}` : '');
    if (reply.added?.length) restartStatus(reply, `Импортировано: ${reply.added.length}`, detail);
    else setStatus('exists', 'Новых записей нет', detail);
  } catch (e) {
    showError(e);
  } finally {
    setBusy(false);
    $('import-file').value = '';
  }
}

function startImport() {
  if (inWindow) {
    $('import-file').click();
    return;
  }
  // A file dialog closes the toolbar popup, so import runs in a small separate window.
  chrome.windows.create({
    url: chrome.runtime.getURL(`popup.html?mode=window&import=1&url=${encodeURIComponent(input.value)}`),
    type: 'popup', width: 380, height: 640,
  });
  window.close();
}

// ---------- wiring ----------

$('add-form').addEventListener('submit', add);
input.addEventListener('input', () => input.removeAttribute('aria-invalid'));
list.addEventListener('click', (e) => {
  const btn = e.target.closest('.item-remove');
  if (btn) remove(btn.closest('.item'));
});
$('export-btn').addEventListener('click', exportList);
$('import-btn').addEventListener('click', startImport);
$('import-file').addEventListener('change', (e) => importFile(e.target.files[0]));
$('retry-btn').addEventListener('click', refresh);
$('copy-cmd').addEventListener('click', async () => {
  await navigator.clipboard.writeText($('install-cmd').textContent.trim());
  $('copy-cmd').dataset.copied = '';
  setTimeout(() => delete $('copy-cmd').dataset.copied, 1500);
});

async function prefill() {
  if (inWindow) {
    input.value = params.get('url') || '';
    return;
  }
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const root = tab?.url ? rootDomainFromUrl(tab.url) : null;
    if (root) input.value = root;
  } catch { /* leave the field empty */ }
  input.focus();
  input.select();
}

prefill();
refresh().then(() => {
  if (inWindow && params.get('import') === '1' && !$('main').hidden) {
    setStatus('info', 'Нажмите «Импорт» и выберите файл', '.txt — по одной записи в строке, или .json');
  }
});
