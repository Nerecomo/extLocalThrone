// Root (registrable) domain of a URL by the Public Suffix List, plus input validation
// mirroring host/throne_db.py normalize_entry().
import { PSL_RULES } from './psl-data.js';

let rules = null;
let wildcards = null;
let exceptions = null;

function load() {
  if (rules) return;
  rules = new Set();
  wildcards = new Set();
  exceptions = new Set();
  for (const r of PSL_RULES.split(' ')) {
    if (r.startsWith('!')) exceptions.add(r.slice(1));
    else if (r.startsWith('*.')) wildcards.add(r.slice(2));
    else rules.add(r);
  }
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

/** Number of labels in the public suffix of `host` (PSL algorithm, default rule "*"). */
function suffixLength(labels) {
  let best = 1;
  for (let i = 0; i < labels.length; i++) {
    const candidate = labels.slice(i).join('.');
    const n = labels.length - i;
    if (exceptions.has(candidate)) return n - 1; // exception wins outright
    if (rules.has(candidate) && n > best) best = n;
    if (i > 0 && wildcards.has(candidate) && n + 1 > best) best = n + 1;
  }
  return best;
}

/** Registrable domain of a hostname, e.g. "www.example.co.uk" -> "example.co.uk". */
export function registrableDomain(hostname) {
  load();
  let host = String(hostname || '').toLowerCase().replace(/\.+$/, '');
  if (host.startsWith('[') && host.endsWith(']')) return host.slice(1, -1); // IPv6 literal
  if (!host || IPV4.test(host) || host.includes(':')) return host;
  const labels = host.split('.');
  const n = suffixLength(labels);
  if (labels.length <= n) return host; // the host is itself a public suffix (or single label)
  return labels.slice(-(n + 1)).join('.');
}

/** Root domain for a tab URL; null for non-web pages (chrome://, file://, about:...). */
export function rootDomainFromUrl(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  return registrableDomain(u.hostname) || null;
}

const LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * Light client-side check before talking to the host (the host validates again).
 * Returns the cleaned value or throws Error with a Russian message.
 * Also accepts a pasted URL and converts it to its hostname.
 */
export function cleanEntry(raw) {
  let s = String(raw ?? '').trim().toLowerCase();
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(s)) {
    try {
      s = registrableDomain(new URL(s).hostname); // a pasted URL is reduced to its root domain, like the prefill
    } catch {
      throw new Error('Не удалось разобрать адрес');
    }
  }
  s = s.replace(/\.+$/, '');
  if (!s) throw new Error('Введите домен, IP или подсеть');
  if (s.length > 253) throw new Error('Слишком длинное значение');
  if (s.includes(':') || /^[\d./]+$/.test(s)) {
    if (/^[0-9a-f:./]+$/.test(s)) return s; // the host checks the exact IP/CIDR syntax
    throw new Error('Некорректный IP-адрес или подсеть');
  }
  let body = s;
  if (body.startsWith('*.')) body = body.slice(2);
  else if (body.startsWith('.')) body = body.slice(1);
  if (!/^[a-z0-9.-]+$/.test(body)) {
    // Allow Unicode domains (only letters/digits/dots/hyphens) by converting them the way the browser does.
    if (/^[\p{L}\p{N}.-]+$/u.test(body)) try {
      const ascii = new URL(`http://${body}`).hostname;
      if (/^[a-z0-9.-]+$/.test(ascii)) return s.slice(0, s.length - body.length) + ascii;
    } catch { /* fall through */ }
    throw new Error('Допустимы только латиница, цифры, точка и дефис');
  }
  const labels = body.split('.');
  if (labels.length < 2 || !labels.every((l) => LABEL.test(l)) || /^\d+$/.test(labels.at(-1))) {
    throw new Error('Некорректный домен');
  }
  return s;
}
