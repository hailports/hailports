#!/usr/bin/env node
/*
 * QuickBooks timecard driver.
 *
 * Guardrails are intentional:
 * - real submit is locked unless qbo_timecard.json says real_submit_enabled + dry_run_approved
 * - browser selectors/default payroll fields must be mapped from Operator's own QBO page
 * - standard hours only; weekends/US holidays skipped; no overtime accepted
 * - override iMessage is mandatory before any real submit
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const HOME = os.homedir();
const ROOT = path.join(HOME, 'claude-stack');
const STATE_PATH = path.join(ROOT, 'data/focus/qbo_timecard.json');
const MAP_JSON = path.join(ROOT, 'data/focus/qbo_timecard_map.json');
const MAP_MD = path.join(ROOT, 'data/focus/qbo_timecard_map.md');
const DIGEST_DIR = path.join(HOME, '.openclaw/workspace/CompanyA-local/digests');
const CDP = process.env.QBO_CDP || 'http://127.0.0.1:18821';
const MSG_DB = path.join(HOME, 'Library/Messages/chat.db');
const TARGET = process.env.QBO_TIMECARD_PHONE || 'XPHONEX';
const WEEKDAY_KEYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'];
const DAY_ABBR = { monday: 'Mon', tuesday: 'Tue', wednesday: 'Wed', thursday: 'Thu', friday: 'Fri' };
const MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const ARGS = process.argv.slice(2);
const has = (x) => ARGS.includes(x);

function defaultState() {
  return {
    status: 'login_required',
    app: 'quickbooks_time',
    fill_entries_enabled: false,
    real_submit_enabled: false,
    dry_run_approved: false,
    timezone: 'America/Chicago',
    cdp: CDP,
    timesheet_url: null,
    override_window_minutes: 30,
    max_daily_hours: 8,
    max_weekly_hours: 40,
    standard_schedule: {
      monday: 8,
      tuesday: 8,
      wednesday: 8,
      thursday: 8,
      friday: 8
    },
    defaults: {
      confirmed: false,
      customer: null,
      service_item: null,
      billable: null,
      class: null,
      notes: ''
    },
    memo: {
      enabled: false,
      update_existing_notes: false,
      max_chars: 220,
      daily_precompute_dir: path.join(HOME, '.openclaw/workspace/CompanyA-local'),
      calendar_digest: path.join(DIGEST_DIR, 'ZOOM_CALENDAR.md'),
      sprint_digest: path.join(DIGEST_DIR, 'sprint_current.md'),
      context_brain_db: path.join(ROOT, 'data/redacted_context_brain.db'),
      work_context_db: path.join(ROOT, 'data/work_context.db'),
      fallback_from_sprint: true
    },
    selectors: {
      mode: 'quickbooks_time_entries_v2',
      week_label: null,
      submitted_indicators: [],
      hours: {},
      customer: {},
      service_item: {},
      class: {},
      notes: {},
      save_button: null,
      submit_button: null,
      autocomplete_enter_fields: []
    },
    submitted_periods: {},
    last_alerts: {},
    history: []
  };
}

function mergeDefaults(base, extra) {
  if (!extra || typeof extra !== 'object') return base;
  for (const [k, v] of Object.entries(extra)) {
    if (v && typeof v === 'object' && !Array.isArray(v) && base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])) {
      base[k] = mergeDefaults(base[k], v);
    } else {
      base[k] = v;
    }
  }
  return base;
}

function loadState() {
  const base = defaultState();
  try {
    return mergeDefaults(base, JSON.parse(fs.readFileSync(STATE_PATH, 'utf8')));
  } catch {
    return base;
  }
}

function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  const tmp = STATE_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2) + '\n');
  fs.renameSync(tmp, STATE_PATH);
}

function note(state, event, detail) {
  state.history = state.history || [];
  state.history.push({ at: new Date().toISOString(), event, detail });
  state.history = state.history.slice(-40);
}

function ymd(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function startOfWeek(date) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const dow = d.getDay();
  const diff = dow === 0 ? -6 : 1 - dow;
  d.setDate(d.getDate() + diff);
  return d;
}

function addDays(date, days) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  d.setDate(d.getDate() + days);
  return d;
}

function dateFromYmd(s) {
  return new Date(`${s}T12:00:00`);
}

function mdy(s) {
  const d = dateFromYmd(s);
  return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}/${d.getFullYear()}`;
}

function qbtDayLabel(s) {
  const d = dateFromYmd(s);
  return `${DAY_ABBR[WEEKDAY_KEYS[d.getDay() - 1]]}, ${MONTH_ABBR[d.getMonth()]} ${d.getDate()}`;
}

function qbtHours(hours) {
  return `${Number(hours).toFixed(2)}`;
}

function isQbt(state) {
  return (state.app || '').includes('quickbooks_time') || state.selectors?.mode === 'quickbooks_time_entries_v2';
}

function nthWeekday(year, month, weekday, nth) {
  const d = new Date(year, month, 1);
  const offset = (weekday - d.getDay() + 7) % 7;
  d.setDate(1 + offset + (nth - 1) * 7);
  return d;
}

function lastWeekday(year, month, weekday) {
  const d = new Date(year, month + 1, 0);
  const offset = (d.getDay() - weekday + 7) % 7;
  d.setDate(d.getDate() - offset);
  return d;
}

function observed(date) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  if (d.getDay() === 6) d.setDate(d.getDate() - 1);
  if (d.getDay() === 0) d.setDate(d.getDate() + 1);
  return d;
}

function usHolidays(year) {
  const raw = [
    new Date(year, 0, 1),
    nthWeekday(year, 0, 1, 3),
    nthWeekday(year, 1, 1, 3),
    lastWeekday(year, 4, 1),
    new Date(year, 5, 19),
    new Date(year, 6, 4),
    nthWeekday(year, 8, 1, 1),
    nthWeekday(year, 9, 1, 2),
    new Date(year, 10, 11),
    nthWeekday(year, 10, 4, 4),
    new Date(year, 11, 25)
  ];
  return new Set(raw.flatMap((d) => [ymd(d), ymd(observed(d))]));
}

function holidaySetForWeek(weekStart) {
  const years = new Set([weekStart.getFullYear(), addDays(weekStart, 6).getFullYear()]);
  const set = new Set();
  for (const year of years) for (const x of usHolidays(year)) set.add(x);
  return set;
}

function periodKey(weekStart) {
  return `${ymd(weekStart)}_${ymd(addDays(weekStart, 6))}`;
}

function buildPlan(state, weekStart) {
  const holidays = holidaySetForWeek(weekStart);
  const entries = [];
  let total = 0;
  for (let i = 0; i < WEEKDAY_KEYS.length; i++) {
    const key = WEEKDAY_KEYS[i];
    const date = addDays(weekStart, i);
    const dateKey = ymd(date);
    const configured = Number(state.standard_schedule?.[key] || 0);
    if (configured <= 0) continue;
    if (holidays.has(dateKey)) continue;
    if (configured > Number(state.max_daily_hours || 8)) {
      throw new Error(`${DAY_ABBR[key]} is ${configured}h; max daily standard is ${state.max_daily_hours}h`);
    }
    entries.push({
      weekday: key,
      label: DAY_ABBR[key],
      date: dateKey,
      hours: configured,
      customer: state.defaults?.customer || null,
      service_item: state.defaults?.service_item || null,
      class: state.defaults?.class || null,
      notes: state.defaults?.notes || ''
    });
    total += configured;
  }
  if (total > Number(state.max_weekly_hours || 40)) throw new Error(`weekly total ${total}h exceeds ${state.max_weekly_hours}h max`);
  return { period: periodKey(weekStart), week_start: ymd(weekStart), week_end: ymd(addDays(weekStart, 6)), entries, total };
}

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return '';
  }
}

function cleanMemoText(s) {
  return String(s || '')
    .replace(/\bshiva\b/ig, "Ravi's team")
    .replace(/\bde Mocskonyi,\s*Operator's\s*/ig, '')
    .replace(/\bAlex's\s*/ig, '')
    .replace(/<[^>]+>/g, '')
    .replace(/\bhold\s*[-:]*\s*/ig, '')
    .replace(/\s+/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .trim();
}

function calendarTitlesForDate(state, dateKey) {
  const text = readText(state.memo?.calendar_digest || path.join(DIGEST_DIR, 'ZOOM_CALENDAR.md'));
  const titles = [];
  for (const line of text.split('\n')) {
    const m = line.match(new RegExp(`^- ${dateKey} \\d{2}:\\d{2} · (.*?) · \\d+m(?:\\b|$)`));
    if (!m) continue;
    const title = cleanMemoText(m[1]);
    if (!title || /personal meeting room/i.test(title)) continue;
    titles.push(title);
  }
  return [...new Set(titles)].slice(0, 12);
}

function dateTokens(dateKey) {
  const d = dateFromYmd(dateKey);
  return [
    dateKey,
    `${MONTH_ABBR[d.getMonth()]} ${d.getDate()}`,
    `${MONTH_ABBR[d.getMonth()]} ${String(d.getDate()).padStart(2, '0')}`
  ];
}

function collectStrings(obj, out = []) {
  if (!obj) return out;
  if (typeof obj === 'string') {
    out.push(obj);
  } else if (Array.isArray(obj)) {
    for (const x of obj) collectStrings(x, out);
  } else if (typeof obj === 'object') {
    for (const x of Object.values(obj)) collectStrings(x, out);
  }
  return out;
}

function evidenceTitle(line) {
  let s = cleanMemoText(line)
    .replace(/^[-*]\s*/, '')
    .replace(/^\[[^\]]+\]\s*/, '')
    .replace(/^new\s+|^read\s+|^unread\s+/i, '');
  const pipe = s.match(/\|\s*([^|]+)$/);
  if (pipe) s = pipe[1];
  s = s
    .replace(/^[^:]{2,60}:\s*/, '')
    .replace(/\s*\((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}[^)]*\).*$/i, '')
    .replace(/\s+[-–—]\s+\d+\s+msgs?.*$/i, '')
    .replace(/\s+last\s+\d{4}-\d{2}-\d{2}.*$/i, '')
    .replace(/^channel\]\s*/i, '')
    .replace(/^dm\]\s*/i, '');
  if (/^(ok|error|source|generated|latency|preview|decision|id|conversation)\b/i.test(s)) return '';
  if (/figma|microsoft 365|newsletter|daily digest|do not reply|no reply/i.test(s)) return '';
  return cleanMemoText(s).slice(0, 160);
}

function precomputeEvidenceForDate(state, dateKey) {
  const dir = state.memo?.daily_precompute_dir || path.join(HOME, '.openclaw/workspace/CompanyA-local');
  const jsonPath = path.join(dir, `daily-precompute-${dateKey}.json`);
  const mdPath = path.join(dir, `daily-precompute-${dateKey}.md`);
  const tokens = dateTokens(dateKey);
  const strings = [];
  try {
    const parsed = JSON.parse(readText(jsonPath));
    collectStrings(parsed.summary?.pressing_items || parsed.summary || parsed.systems || parsed, strings);
  } catch {
    strings.push(readText(mdPath));
  }
  const out = [];
  for (const block of strings) {
    for (const raw of String(block || '').split('\n')) {
      if (!tokens.some((token) => raw.includes(token))) continue;
      const title = evidenceTitle(raw);
      if (title) out.push(title);
    }
  }
  return [...new Set(out)].slice(0, 18);
}

function sqlLiteral(s) {
  return `'${String(s || '').replace(/'/g, "''")}'`;
}

function memoTerms(texts) {
  const joined = texts.join(' ');
  const preferred = ['Salesforce', 'SFDC', 'DPP', 'SPP', 'SP', 'Portal', 'UAT', 'Warranty', 'Commissioning', 'Claims', 'Parts', 'Dealer', 'Grower', 'Pricing', 'Quotes', 'Data', 'Migration'];
  const out = preferred.filter((term) => new RegExp(`\\b${term}\\b`, 'i').test(joined));
  if (out.length) return out.slice(0, 8);
  return joined.split(/\W+/).filter((w) => w.length > 4).slice(0, 8);
}

function sqliteRows(dbPath, sql) {
  if (!dbPath || !fs.existsSync(dbPath)) return [];
  try {
    return cp.execFileSync('/usr/bin/sqlite3', ['-readonly', dbPath, sql], { encoding: 'utf8', timeout: 8000, stdio: ['ignore', 'pipe', 'ignore'] })
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean);
  } catch {
    return [];
  }
}

function contextBrainEvidence(state, seedTexts) {
  const db = state.memo?.context_brain_db || path.join(ROOT, 'data/redacted_context_brain.db');
  const terms = memoTerms(seedTexts).map((t) => t.replace(/[^A-Za-z0-9]/g, '')).filter((t) => t.length > 1);
  if (!terms.length) return [];
  const match = terms.slice(0, 8).map((t) => `"${t}"`).join(' OR ');
  const sql = `SELECT replace(m.title || ' ' || substr(m.body,1,280), char(10), ' ') FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid WHERE memories_fts MATCH ${sqlLiteral(match)} ORDER BY m.pinned DESC, m.updated_at DESC LIMIT 6;`;
  return sqliteRows(db, sql).map(evidenceTitle).filter(Boolean);
}

function workContextEvidence(state, seedTexts) {
  const db = state.memo?.work_context_db || path.join(ROOT, 'data/work_context.db');
  const terms = memoTerms(seedTexts).map((t) => t.replace(/[^A-Za-z0-9]/g, '')).filter((t) => t.length > 1);
  if (!terms.length) return [];
  const match = terms.slice(0, 8).map((t) => `"${t}"`).join(' OR ');
  const sql = `SELECT replace(title || ' ' || substr(body,1,220), char(10), ' ') FROM docs WHERE docs MATCH ${sqlLiteral(match)} ORDER BY bm25(docs, 0.0, 0.0, 0.0, 0.0, 5.0, 3.0, 1.0) LIMIT 8;`;
  return sqliteRows(db, sql).map(evidenceTitle).filter(Boolean);
}

function addMemoTopic(out, title) {
  const t = title.toLowerCase();
  if (/uat|user acceptance|testing|test case/.test(t)) out.add('UAT support/testing');
  if (/sprint|backlog|stand-?up|salesforce|sfdc/.test(t)) out.add('Salesforce sprint coordination');
  if (/warranty|commissioning|claims?|parts return|shipping|receiving/.test(t)) out.add('warranty/claims workflow review');
  if (/dpp|dealer|grower|sp portal|strategic partner|rebate/.test(t)) out.add('dealer/DPP portal work');
  if (/australia parts|parts website|windchill|acd/.test(t)) out.add('parts website testing');
  if (/data|migration|databricks|master data/.test(t)) out.add('data/migration validation');
  if (/ticket|issue|support|request/.test(t)) out.add('support issue triage');
  if (/1x1|1:1|sync|check.?in|leaders|leadership|follow.?up|replanning/.test(t)) out.add('stakeholder syncs and follow-ups');
  if (/training|assessment|process/.test(t)) out.add('process training/review');
  if (/quote|quoting|pricing|market support|discount/.test(t)) out.add('pricing/quoting support');
}

function sprintFallbackMemo(state) {
  if (state.memo?.fallback_from_sprint === false) return '';
  const text = readText(state.memo?.sprint_digest || path.join(DIGEST_DIR, 'sprint_current.md'));
  if (!text) return '';
  const topics = [];
  if (/SP Portal/i.test(text)) topics.push('SP Portal');
  if (/\bDPP\b/i.test(text)) topics.push('DPP validation');
  if (/Warranty|Commissioning/i.test(text)) topics.push('warranty/commissioning');
  if (/Pricing|Quotes?/i.test(text)) topics.push('pricing/quoting');
  if (!topics.length) return '';
  return `Salesforce sprint work: ${topics.slice(0, 4).join(', ')}`;
}

function memoForDate(state, dateKey) {
  const configured = cleanMemoText(state.defaults?.notes || '');
  if (!state.memo?.enabled) return configured;
  const titles = calendarTitlesForDate(state, dateKey);
  const precompute = precomputeEvidenceForDate(state, dateKey);
  const context = contextBrainEvidence(state, [...titles, ...precompute]);
  const workContext = workContextEvidence(state, [...titles, ...precompute, ...context]);
  const evidence = [...titles, ...precompute, ...context, ...workContext];
  const topics = new Set();
  for (const title of evidence) addMemoTopic(topics, title);
  let memo = '';
  if (topics.size) {
    memo = Array.from(topics).slice(0, 4).join('; ');
  } else if (evidence.length) {
    memo = evidence.slice(0, 3).join('; ');
  } else {
    memo = sprintFallbackMemo(state);
  }
  if (configured && memo) memo = `${configured}; ${memo}`;
  if (!memo) memo = configured;
  const max = Number(state.memo?.max_chars || 220);
  return cleanMemoText(memo).slice(0, max).replace(/[;,:\s]+$/, '');
}

function attachMemos(plan, state) {
  for (const entry of plan.entries) {
    entry.notes = memoForDate(state, entry.date);
  }
  return plan;
}

function planText(plan, state, label) {
  const defaults = [
    state.defaults?.customer ? `customer=${state.defaults.customer}` : 'customer=(unknown)',
    state.defaults?.service_item ? `service=${state.defaults.service_item}` : 'service=(unknown)',
    state.defaults?.class ? `class=${state.defaults.class}` : 'class=(none/unknown)'
  ].join(' | ');
  const lines = [
    `QBO timecard ${label}: ${plan.week_start} to ${plan.week_end}`,
    defaults,
    ...plan.entries.map((e) => `${e.label} ${e.date}: ${e.hours}h${e.notes ? ` | memo: ${e.notes}` : ''}`),
    `total: ${plan.total}h`
  ];
  return lines.join('\n');
}

function blockersFor(state, plan, real) {
  const b = [];
  const qbt = isQbt(state);
  if (!state.timesheet_url) b.push('timesheet_url not mapped');
  if (!state.defaults?.confirmed) b.push('customer/service/class defaults not confirmed');
  if (qbt) {
    if (!state.defaults?.customer) b.push('customer default missing');
    if (!state.defaults?.service_item) b.push('service item default missing');
    if (!state.defaults?.billable) b.push('billable default missing');
    if ((has('--fill-only') || has('--notes-only')) && !state.fill_entries_enabled) b.push('fill_entries_enabled=false');
  } else {
    for (const e of plan.entries) {
      if (!state.selectors?.hours?.[e.weekday] && !state.selectors?.hours?.[e.date]) b.push(`hours selector missing for ${e.label}`);
    }
  }
  if (real) {
    if (!state.real_submit_enabled) b.push('real_submit_enabled=false');
    if (!state.dry_run_approved) b.push('dry_run_approved=false');
    if (state.status !== 'ready_for_real_submit') b.push(`status=${state.status}`);
    if (!state.selectors?.submit_button) b.push('submit_button selector missing');
    if (!state.selectors?.week_label) b.push('week_label selector missing');
  }
  return [...new Set(b)];
}

async function connect() {
  let browser;
  try {
    browser = await chromium.connectOverCDP(CDP);
  } catch (e) {
    const err = new Error(`cannot connect ${CDP}; run bash scripts/login_capture_quickbooks.sh and sign in once`);
    err.code = 'NO_CDP';
    throw err;
  }
  const ctx = browser.contexts()[0] || await browser.newContext();
  let page = ctx.pages().find((p) => /intuit|qbo|quickbooks/i.test(p.url())) || ctx.pages()[0] || await ctx.newPage();
  return { browser, ctx, page };
}

async function moveWindowsOffscreen(ctx) {
  const seen = new Set();
  for (const pg of ctx.pages()) {
    try {
      const session = await ctx.newCDPSession(pg);
      const { windowId } = await session.send('Browser.getWindowForTarget');
      if (!seen.has(windowId)) {
        seen.add(windowId);
        const { bounds } = await session.send('Browser.getWindowBounds', { windowId });
        if ((bounds.left || 0) > -5000) {
          await session.send('Browser.setWindowBounds', { windowId, bounds: { left: -32000, top: -32000 } });
        }
      }
      await session.detach();
    } catch {}
  }
}

async function looksLoggedOut(page) {
  const url = page.url();
  if (/accounts\.intuit\.com|signin|sign-in|login/i.test(url) && !/app\.qbo\.intuit\.com/i.test(url)) return true;
  let text = '';
  try { text = await page.locator('body').innerText({ timeout: 4000 }); } catch {}
  return /sign in to quickbooks|verify your identity|enter the code|intuit account/i.test(text);
}

function cssEscape(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

async function mapFlow(state) {
  const { browser, ctx, page } = await connect();
  await moveWindowsOffscreen(ctx);
  try {
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 }).catch(() => {});
    const loggedOut = await looksLoggedOut(page);
    const info = await page.evaluate(() => {
      function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
      function selector(el) {
        const data = ['data-testid', 'data-test-id', 'data-automation-id', 'data-qbo-id', 'aria-label', 'name', 'id'];
        for (const a of data) {
          const v = el.getAttribute(a);
          if (v) {
            if (a === 'id') return `#${CSS.escape(v)}`;
            return `${el.tagName.toLowerCase()}[${a}="${CSS.escape(v)}"]`;
          }
        }
        const txt = clean(el.innerText || el.value || el.placeholder).slice(0, 50);
        if (txt && (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button')) return `text=${txt}`;
        const parts = [];
        let cur = el;
        while (cur && cur.nodeType === 1 && parts.length < 5) {
          let p = cur.tagName.toLowerCase();
          if (cur.id) { parts.unshift(`#${CSS.escape(cur.id)}`); break; }
          const parent = cur.parentElement;
          if (parent) {
            const same = Array.from(parent.children).filter((x) => x.tagName === cur.tagName);
            if (same.length > 1) p += `:nth-of-type(${same.indexOf(cur) + 1})`;
          }
          parts.unshift(p);
          cur = parent;
        }
        return parts.join(' > ');
      }
      const controls = Array.from(document.querySelectorAll('input, textarea, select, button, [role="button"], a'))
        .filter((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        })
        .slice(0, 180)
        .map((el) => ({
          tag: el.tagName.toLowerCase(),
          type: el.getAttribute('type') || '',
          role: el.getAttribute('role') || '',
          text: clean(el.innerText || el.value || ''),
          aria: el.getAttribute('aria-label') || '',
          placeholder: el.getAttribute('placeholder') || '',
          name: el.getAttribute('name') || '',
          id: el.id || '',
          selector: selector(el)
        }));
      const tables = Array.from(document.querySelectorAll('table')).slice(0, 8).map((tbl) =>
        Array.from(tbl.querySelectorAll('tr')).slice(0, 12).map((tr) =>
          Array.from(tr.querySelectorAll('th,td')).slice(0, 12).map((td) => clean(td.innerText).slice(0, 90))
        )
      );
      return {
        url: location.href,
        title: document.title,
        body_sample: clean(document.body?.innerText || '').slice(0, 5000),
        controls,
        tables
      };
    });
    info.generated = new Date().toISOString();
    info.logged_out_guess = loggedOut;
    fs.mkdirSync(path.dirname(MAP_JSON), { recursive: true });
    fs.writeFileSync(MAP_JSON, JSON.stringify(info, null, 2) + '\n');
    const lines = [
      '# QBO Timecard Map',
      '',
      `generated: ${info.generated}`,
      `url: ${info.url}`,
      `title: ${info.title || ''}`,
      `logged_out_guess: ${loggedOut}`,
      '',
      '## Candidate Controls',
      ...info.controls.slice(0, 120).map((c) => `- ${c.tag}${c.type ? `[${c.type}]` : ''} ${c.text || c.aria || c.placeholder || c.name || c.id || '(blank)'} :: ${c.selector}`),
      '',
      '## Table Samples',
      ...info.tables.flatMap((rows, i) => [`table ${i + 1}`, ...rows.map((r) => `- ${r.join(' | ')}`), ''])
    ];
    fs.writeFileSync(MAP_MD, lines.join('\n') + '\n');
    state.timesheet_url = info.url;
    state.status = loggedOut ? 'login_required' : 'needs_selector_config';
    state.last_map_at = info.generated;
    note(state, 'map', loggedOut ? 'login_required' : info.url);
    saveState(state);
    console.log(`mapped -> ${MAP_MD}`);
    if (loggedOut) console.log('blocked: QuickBooks login/MFA still needed');
  } finally {
    await browser.close().catch(() => {});
  }
}

function selectorFor(map, entry) {
  if (!map) return null;
  return map[entry.date] || map[entry.weekday] || null;
}

async function readValue(page, selector) {
  if (!selector) return '';
  const loc = page.locator(selector).first();
  if (!(await loc.count().catch(() => 0))) return '';
  const tag = await loc.evaluate((el) => el.tagName.toLowerCase()).catch(() => '');
  if (['input', 'textarea', 'select'].includes(tag)) return String(await loc.inputValue().catch(() => '')).trim();
  return String(await loc.innerText().catch(() => '')).trim();
}

function parseHours(v) {
  const m = String(v || '').match(/(\d+(?:\.\d+)?)/);
  return m ? Number(m[1]) : 0;
}

async function visibleAny(page, selectors) {
  for (const s of selectors || []) {
    try {
      if (await page.locator(s).first().isVisible({ timeout: 1500 })) return s;
    } catch {}
  }
  return null;
}

async function openTimesheet(page, state) {
  if (!state.timesheet_url) return;
  if (page.url() !== state.timesheet_url) {
    await page.goto(state.timesheet_url, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  }
  await page.waitForTimeout(3000);
  if (isQbt(state) && !(await page.locator('#time-entries-list').count().catch(() => 0))) {
    await page.locator('#timesheets_v2_shortcut').click({ timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(5000);
  }
}

async function closeQbtEditors(page) {
  await page.keyboard.press('Escape').catch(() => {});
  await page.locator('#tt_jobcode_select_timesheet_edit_close_winc').click({ timeout: 1500, force: true }).catch(() => {});
  await page.locator('#timesheet_edit_cancel_button').click({ timeout: 1500, force: true }).catch(() => {});
  await page.locator('#timesheet_edit_close_winc').click({ timeout: 1500, force: true }).catch(() => {});
  await page.waitForTimeout(700);
}

async function readExistingQbt(page, plan) {
  await closeQbtEditors(page);
  const rows = await page.evaluate((entries) => {
    const root = document.querySelector('#time-entries-list') || document.body;
    const text = (root.innerText || '').replace(/\s+/g, ' ');
    return entries.map((entry) => {
      const pattern = new RegExp(entry.qbt_label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s+(\\d+)h\\s+(\\d+)m', 'i');
      const m = text.match(pattern);
      const hours = m ? Number(m[1]) + Number(m[2]) / 60 : 0;
      return { date: entry.date, existing_value: m ? `${m[1]}h ${m[2]}m` : '', existing_hours: hours };
    });
  }, plan.entries.map((e) => ({ date: e.date, qbt_label: qbtDayLabel(e.date) })));
  return {
    submitted: /submitted|approved/i.test(await page.locator('body').innerText({ timeout: 3000 }).catch(() => '')),
    submitted_selector: null,
    rows: plan.entries.map((e) => {
      const found = rows.find((r) => r.date === e.date) || {};
      return { ...e, ...found, already_filled: Number(found.existing_hours || 0) > 0 };
    })
  };
}

async function readExisting(page, state, plan) {
  if (isQbt(state)) return readExistingQbt(page, plan);
  const submittedHit = await visibleAny(page, state.selectors?.submitted_indicators || []);
  const rows = [];
  for (const e of plan.entries) {
    const sel = selectorFor(state.selectors?.hours, e);
    const val = sel ? await readValue(page, sel) : '';
    const hours = parseHours(val);
    rows.push({ ...e, selector: sel, existing_value: val, existing_hours: hours, already_filled: hours > 0 });
  }
  return { submitted: !!submittedHit, submitted_selector: submittedHit, rows };
}

async function setValue(page, selector, value, field, state) {
  if (!selector || value === null || value === undefined || value === '') return;
  const loc = page.locator(selector).first();
  if (!(await loc.count().catch(() => 0))) throw new Error(`selector not found for ${field}: ${selector}`);
  await loc.scrollIntoViewIfNeeded().catch(() => {});
  const tag = await loc.evaluate((el) => el.tagName.toLowerCase()).catch(() => '');
  if (tag === 'select') {
    await loc.selectOption({ label: String(value) }).catch(async () => loc.selectOption(String(value)));
  } else {
    await loc.click({ timeout: 15000 });
    await loc.fill(String(value), { timeout: 15000 });
    if ((state.selectors?.autocomplete_enter_fields || []).includes(field)) {
      await loc.press('Enter').catch(() => {});
    }
  }
}

async function fillEntries(page, state, rows) {
  if (isQbt(state)) return fillQbtEntries(page, state, rows);
  for (const row of rows) {
    if (row.already_filled) continue;
    await setValue(page, selectorFor(state.selectors?.hours, row), String(row.hours), 'hours', state);
    await setValue(page, selectorFor(state.selectors?.customer, row), row.customer, 'customer', state);
    await setValue(page, selectorFor(state.selectors?.service_item, row), row.service_item, 'service_item', state);
    await setValue(page, selectorFor(state.selectors?.class, row), row.class, 'class', state);
    await setValue(page, selectorFor(state.selectors?.notes, row), row.notes, 'notes', state);
  }
  if (state.selectors?.save_button) {
    await page.locator(state.selectors.save_button).first().click({ timeout: 20000 });
    await page.waitForTimeout(5000);
  }
}

async function selectQbtCustomer(page, customer) {
  await page.locator('#timesheet_edit_jobcode_display').click({ timeout: 15000 });
  await page.waitForTimeout(800);
  const search = page.locator('#jobcode_select_search_input');
  if (await search.count().catch(() => 0)) {
    await search.fill(customer);
    await page.waitForTimeout(600);
  }
  const option = page.locator('#jobcode_select_body_job_list_content li[role="option"]').filter({ hasText: customer }).first();
  await option.click({ timeout: 15000 });
  await page.waitForTimeout(800);
}

async function setQbtDate(page, dateKey) {
  const target = mdy(dateKey);
  const result = await page.evaluate((value) => {
    const el = document.querySelector('#timesheet_edit_start_date');
    if (!el) return { ok: false, value: '' };
    if (window.jQuery && window.jQuery(el).datepicker) window.jQuery(el).datepicker('setDate', value);
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
    return { ok: el.value === value, value: el.value };
  }, target);
  if (!result.ok) throw new Error(`QBO date did not stick for ${dateKey}; field=${result.value}`);
  await page.keyboard.press('Escape').catch(() => {});
}

async function fillQbtEntries(page, state, rows) {
  const toFill = rows.filter((row) => !row.already_filled);
  for (const row of toFill) {
    await closeQbtEditors(page);
    await page.locator('#timesheets_v2_add_timesheet_button').click({ timeout: 15000 });
    await page.waitForTimeout(1200);
    await page.locator('#new_timesheet_manual_radio').click({ timeout: 10000 }).catch(() => {});
    await page.locator('#timesheet_edit_total_time').fill(qbtHours(row.hours), { timeout: 10000 });
    await page.keyboard.press('Escape').catch(() => {});
    await page.locator('#timesheet_edit_total_time').click({ timeout: 5000, force: true }).catch(() => {});
    await page.keyboard.press('Escape').catch(() => {});
    await selectQbtCustomer(page, row.customer || state.defaults.customer);
    await page.locator('#timesheet_edit_v_4874554').selectOption({ label: row.service_item || state.defaults.service_item });
    await page.locator('#timesheet_edit_v_4874558').selectOption({ label: state.defaults.billable });
    if (row.notes || state.defaults.notes) await page.locator('#tse_notes').fill(row.notes || state.defaults.notes);
    await setQbtDate(page, row.date);
    if (await page.locator('#timesheet_edit_leave_editor_open_checkbox').isChecked().catch(() => false)) {
      await page.locator('#timesheet_edit_leave_editor_open_checkbox').click({ timeout: 5000, force: true }).catch(() => {});
    }
    const dateBeforeSave = await page.locator('#timesheet_edit_start_date').inputValue({ timeout: 5000 });
    if (dateBeforeSave !== mdy(row.date)) throw new Error(`refusing save: expected ${mdy(row.date)}, got ${dateBeforeSave}`);
    await page.locator('#timesheet_edit_save_button').click({ timeout: 15000 });
    await page.waitForTimeout(2500);
  }
  await closeQbtEditors(page);
}

async function updateExistingQbtNotes(page, rows) {
  const changed = [];
  const skipped = [];
  for (const row of rows.filter((r) => r.already_filled && r.notes)) {
    await closeQbtEditors(page);
    const edit = page.locator(`button[aria-label*="Edit ${row.date}"]`).first();
    if (!(await edit.count().catch(() => 0))) {
      skipped.push(`${row.date}: edit button missing`);
      continue;
    }
    await edit.click({ timeout: 15000 });
    await page.waitForTimeout(1200);
    const notes = page.locator('#tse_notes').first();
    if (!(await notes.count().catch(() => 0))) {
      skipped.push(`${row.date}: notes field missing`);
      await closeQbtEditors(page);
      continue;
    }
    const current = String(await notes.inputValue({ timeout: 5000 }).catch(() => '')).trim();
    if (current && current !== row.notes) {
      skipped.push(`${row.date}: existing memo kept`);
      await closeQbtEditors(page);
      continue;
    }
    if (current === row.notes) {
      await closeQbtEditors(page);
      continue;
    }
    await notes.fill(row.notes, { timeout: 10000 });
    const dateBeforeSave = await page.locator('#timesheet_edit_start_date').inputValue({ timeout: 5000 }).catch(() => '');
    if (dateBeforeSave && dateBeforeSave !== mdy(row.date)) throw new Error(`refusing memo save: expected ${mdy(row.date)}, got ${dateBeforeSave}`);
    await page.locator('#timesheet_edit_save_button').click({ timeout: 15000 });
    await page.waitForTimeout(2200);
    changed.push(row.date);
  }
  await closeQbtEditors(page);
  return { changed, skipped };
}

function sendIMessage(body) {
  if (has('--no-imessage')) return false;
  const py = [
    'import sys',
    `sys.path.insert(0, ${JSON.stringify(ROOT)})`,
    'from tools.imsg_bridge import send_imessage',
    'send_imessage(sys.stdin.read())'
  ].join('; ');
  const r = cp.spawnSync('/usr/bin/python3', ['-c', py], { input: body, encoding: 'utf8', timeout: 35000 });
  if (r.error || r.status !== 0) {
    const r2 = cp.spawnSync('/opt/homebrew/bin/python3.12', ['-c', py], { input: body, encoding: 'utf8', timeout: 35000 });
    return !r2.error && r2.status === 0;
  }
  return true;
}

function sqlite(sql) {
  try {
    return cp.execFileSync('/usr/bin/sqlite3', ['-readonly', MSG_DB, sql], { encoding: 'utf8', timeout: 10000 }).trim();
  } catch {
    return '';
  }
}

function maxMessageRow() {
  return Number(sqlite('SELECT coalesce(MAX(ROWID),0) FROM message;') || 0);
}

function targetHandleIds() {
  const alt = TARGET.replace(/^\+1/, '');
  const out = sqlite(`SELECT ROWID FROM handle WHERE id = '${TARGET.replace(/'/g, "''")}' OR id = '${alt.replace(/'/g, "''")}';`);
  return out.split(/\s+/).filter(Boolean);
}

function readReplies(afterRow) {
  const ids = targetHandleIds();
  if (!ids.length) return [];
  const sql = `SELECT ROWID || char(9) || replace(replace(text, char(10), ' '), char(9), ' ') FROM message WHERE ROWID > ${Number(afterRow) || 0} AND is_from_me = 0 AND handle_id IN (${ids.join(',')}) AND text IS NOT NULL ORDER BY ROWID ASC;`;
  const out = sqlite(sql);
  if (!out) return [];
  return out.split('\n').map((line) => {
    const [rowid, ...rest] = line.split('\t');
    return { rowid: Number(rowid), text: rest.join('\t') };
  }).filter((x) => x.text);
}

function parseOverride(text, plan, state) {
  const t = String(text || '').toLowerCase();
  if (/\b(skip|hold|cancel|stop|do not submit|dont submit|don't submit)\b/.test(t)) return { action: 'hold', reason: text };
  if (/\b(ok|okay|yes|approved|approve|submit|go|no changes|looks right)\b/.test(t)) return { action: 'proceed' };
  const updates = {};
  const aliases = {
    monday: ['mon', 'monday'],
    tuesday: ['tue', 'tues', 'tuesday'],
    Wednesday: ['wed', 'weds', 'wednesday'],
    thursday: ['thu', 'thur', 'thurs', 'thursday'],
    friday: ['fri', 'friday']
  };
  for (const [day, words] of Object.entries(aliases)) {
    const group = words.join('|');
    const off = new RegExp(`\\b(${group})\\b[^.\\n,;]{0,30}\\b(off|zero|0h|0)\\b`, 'i');
    if (off.test(text)) updates[day.toLowerCase()] = 0;
    const r1 = new RegExp(`\\b(${group})\\b[^0-9\\n,;]{0,25}(\\d+(?:\\.\\d+)?)\\s*h?\\b`, 'ig');
    const r2 = new RegExp(`\\b(\\d+(?:\\.\\d+)?)\\s*h?\\s*(?:on\\s*)?(${group})\\b`, 'ig');
    let m;
    while ((m = r1.exec(text))) updates[day.toLowerCase()] = Number(m[2]);
    while ((m = r2.exec(text))) updates[day.toLowerCase()] = Number(m[1]);
  }
  if (!Object.keys(updates).length) return { action: 'unknown', reason: text };
  const max = Number(state.max_daily_hours || 8);
  const adjusted = JSON.parse(JSON.stringify(plan));
  for (const entry of adjusted.entries) {
    if (Object.prototype.hasOwnProperty.call(updates, entry.weekday)) entry.hours = updates[entry.weekday];
    if (entry.hours < 0 || entry.hours > max) return { action: 'invalid', reason: `${entry.label} ${entry.hours}h exceeds standard max ${max}h` };
  }
  adjusted.total = adjusted.entries.reduce((n, e) => n + Number(e.hours || 0), 0);
  if (adjusted.total > Number(state.max_weekly_hours || 40)) return { action: 'invalid', reason: `weekly total ${adjusted.total}h exceeds ${state.max_weekly_hours}h` };
  adjusted.entries = adjusted.entries.filter((e) => Number(e.hours || 0) > 0);
  return { action: 'adjust', plan: adjusted, raw: text };
}

async function overrideGate(plan, state, dryRun) {
  const msg = dryRun
    ? `${planText(plan, state, 'dry-run')}\n\nDry-run only: nothing will be submitted. Reply with changes, skip/hold, or ok so I can validate the parser.`
    : `${planText(plan, state, 'ready to submit')}\n\nReply within ${state.override_window_minutes} min with changes, skip/hold, or ok. No reply = submit these standard hours.`;
  const before = maxMessageRow();
  sendIMessage(msg);
  if (dryRun || has('--no-wait')) return { action: 'proceed', dry: true };
  const deadline = Date.now() + Number(state.override_window_minutes || 30) * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, 15000));
    const replies = readReplies(before);
    if (!replies.length) continue;
    const parsed = parseOverride(replies[0].text, plan, state);
    if (parsed.action === 'unknown') {
      sendIMessage(`I could not parse this QBO timecard override: "${replies[0].text}". Holding; no submit.`);
      return { action: 'hold', reason: 'unparsed override' };
    }
    if (parsed.action === 'invalid') {
      sendIMessage(`QBO timecard held: ${parsed.reason}. No submit.`);
      return { action: 'hold', reason: parsed.reason };
    }
    return parsed;
  }
  return { action: 'proceed' };
}

function maybeAlert(state, key, body, cooldownHours = 24) {
  const now = Date.now();
  const last = Number(state.last_alerts?.[key] || 0);
  if (cooldownHours === 'once' && last) return false;
  if (now - last < cooldownHours * 3600 * 1000) return false;
  state.last_alerts = state.last_alerts || {};
  state.last_alerts[key] = now;
  sendIMessage(body);
  return true;
}

async function runDriver(state) {
  const fillOnly = has('--fill-only');
  const notesOnly = has('--notes-only');
  const requestedReal = has('--submit') || has('--real') || has('--scheduled') || fillOnly || notesOnly;
  const dryRun = has('--dry') || !requestedReal || (!fillOnly && !notesOnly && !state.real_submit_enabled);
  const weekArg = ARGS.find((a) => /^\d{4}-\d{2}-\d{2}$/.test(a));
  const weekStart = startOfWeek(weekArg ? new Date(`${weekArg}T12:00:00`) : new Date());
  const plan = attachMemos(buildPlan(state, weekStart), state);
  if (!plan.entries.length) {
    console.log(`skip: no weekday standard hours for ${plan.week_start} to ${plan.week_end}`);
    return;
  }

  const blockers = blockersFor(state, plan, requestedReal && !dryRun && !fillOnly && !notesOnly);
  if (blockers.length) {
    console.log(planText(plan, state, dryRun ? 'blocked dry-run' : 'blocked real-submit'));
    console.log('blocked: ' + blockers.join('; '));
    note(state, 'blocked', blockers.join('; '));
    if (has('--scheduled')) maybeAlert(state, 'not-ready', `QBO timecard is not ready: ${blockers.join('; ')}. Run the QuickBooks login + mapping step, then confirm defaults/dry-run.`);
    saveState(state);
    return;
  }

  let browser, ctx, page;
  try {
    ({ browser, ctx, page } = await connect());
    await moveWindowsOffscreen(ctx);
    await openTimesheet(page, state);
    if (await looksLoggedOut(page)) {
      state.status = 'login_required';
      note(state, 'login_required', page.url());
      if (has('--scheduled')) maybeAlert(state, 'login-required', 'QuickBooks timecard login expired. Run: bash scripts/login_capture_quickbooks.sh, sign in with Intuit/MFA, open the timesheet, and leave it open.', 'once');
      saveState(state);
      console.log('blocked: QuickBooks login required');
      return;
    }
    delete state.last_alerts['login-required'];
    delete state.last_alerts['no-cdp'];
    const existing = await readExisting(page, state, plan);
    if (existing.submitted || state.submitted_periods?.[plan.period]?.status === 'submitted') {
      console.log(`skip: period already submitted ${plan.period}`);
      note(state, 'already_submitted', plan.period);
      saveState(state);
      return;
    }
    const conflicts = existing.rows.filter((r) => r.already_filled && Math.abs(r.existing_hours - r.hours) > 0.01);
    if (conflicts.length) {
      const detail = conflicts.map((r) => `${r.label} existing=${r.existing_value} planned=${r.hours}`).join('; ');
      console.log('hold: existing hours conflict; ' + detail);
      maybeAlert(state, `conflict-${plan.period}`, `QBO timecard held: existing hours conflict. ${detail}`);
      note(state, 'conflict', detail);
      saveState(state);
      return;
    }
    console.log(planText(plan, state, dryRun ? 'dry-run' : 'ready'));
    console.log('existing: ' + existing.rows.map((r) => `${r.label}=${r.existing_value || 'blank'}`).join(', '));

    const gate = await overrideGate(plan, state, dryRun);
    if (dryRun) {
      note(state, 'dry_run', plan.period);
      saveState(state);
      console.log('dry-run only: no fields changed, no submit clicked');
      return;
    }
    if (gate.action === 'hold') {
      state.submitted_periods[plan.period] = { status: 'held', at: new Date().toISOString(), reason: gate.reason || 'override hold' };
      note(state, 'held', gate.reason || 'override');
      saveState(state);
      console.log('held: override');
      return;
    }
    const finalPlan = gate.action === 'adjust' ? gate.plan : plan;
    const reread = await readExisting(page, state, finalPlan);
    if (notesOnly) {
      const notesResult = isQbt(state) ? await updateExistingQbtNotes(page, reread.rows) : { changed: [], skipped: ['notes-only is only mapped for QuickBooks Time'] };
      state.submitted_periods[finalPlan.period] = {
        status: 'notes_updated_unsubmitted',
        at: new Date().toISOString(),
        total: finalPlan.total,
        rows: finalPlan.entries,
        notes_changed: notesResult.changed,
        notes_skipped: notesResult.skipped
      };
      note(state, 'notes_updated_unsubmitted', `${finalPlan.period}: ${notesResult.changed.length} changed`);
      saveState(state);
      console.log(`memo update: ${notesResult.changed.length} changed, ${notesResult.skipped.length} skipped; no hours changed, submit not clicked`);
      return;
    }
    await fillEntries(page, state, reread.rows);
    let notesResult = { changed: [], skipped: [] };
    if (fillOnly && isQbt(state) && state.memo?.update_existing_notes) {
      notesResult = await updateExistingQbtNotes(page, reread.rows);
      if (notesResult.changed.length || notesResult.skipped.length) console.log(`memo update: ${notesResult.changed.length} changed, ${notesResult.skipped.length} skipped`);
    }
    if (fillOnly) {
      const afterFill = await readExisting(page, state, finalPlan);
      const okFill = afterFill.rows.every((r) => Math.abs(Number(r.existing_hours || 0) - r.hours) < 0.01);
      state.submitted_periods[finalPlan.period] = {
        status: okFill ? 'filled_unsubmitted' : 'fill_verify_needed',
        at: new Date().toISOString(),
        total: finalPlan.total,
        rows: finalPlan.entries,
        notes_changed: notesResult.changed,
        notes_skipped: notesResult.skipped
      };
      note(state, okFill ? 'filled_unsubmitted' : 'fill_verify_needed', finalPlan.period);
      saveState(state);
      console.log(okFill ? 'filled missing entries; submit not clicked' : 'filled entries; readback verify needed; submit not clicked');
      return;
    }
    // QBO Time submit flow (validated 2026-07-10). The primary "Submit Time"
    // button opens a week panel; you MUST tick the per-day checkboxes that carry
    // hours, click "Submit Week", then confirm "Submit" in the dialog. A blind
    // single click on a "Submit Time" text match no-ops (it hits the hidden
    // "Review & Submit Time" button) and silently submits nothing.
    const openBtn = page.locator('button.action-menu-submit', { hasText: 'Submit Time' }).first();
    await openBtn.click({ timeout: 25000 });
    await page.waitForTimeout(2500);
    await page.locator('.modal button:has-text("Close"), [role=dialog] button:has-text("Close")').first().click({ timeout: 1500 }).catch(() => {});
    const dayBoxes = page.locator('[role=checkbox].week-day');
    const dcount = await dayBoxes.count();
    let picked = 0;
    for (let i = 0; i < dcount; i++) {
      const d = dayBoxes.nth(i);
      const t = (await d.innerText().catch(() => '')).replace(/\n/g, ' ');
      if (/\b[1-9]\d*h\b/.test(t)) { // day has real hours (skip 0m / --)
        if ((await d.getAttribute('aria-checked')) !== 'true') { await d.click({ timeout: 8000 }); await page.waitForTimeout(200); }
        picked++;
      }
    }
    if (!picked) throw new Error('submit: no day checkboxes with hours to select');
    await page.locator('button.new-submit-time', { hasText: 'Submit Week' }).first().click({ timeout: 15000 });
    await page.waitForTimeout(2500);
    await page.locator('[role=dialog] button, .modal button').filter({ hasText: /^\s*submit\s*$/i }).first().click({ timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(5000);
    const lockedCount = await page.locator('[class*="submitted"], .locked').count().catch(() => 0);
    const after = await readExisting(page, state, finalPlan);
    const okHours = after.rows.every((r) => Math.abs(Number(r.existing_hours || 0) - r.hours) < 0.01);
    const okSubmit = okHours && lockedCount > 0;
    state.submitted_periods[finalPlan.period] = {
      status: okSubmit ? 'submitted' : 'verify_needed',
      at: new Date().toISOString(),
      total: finalPlan.total,
      days_selected: picked,
      rows: finalPlan.entries
    };
    note(state, okSubmit ? 'submitted' : 'verify_needed', finalPlan.period);
    saveState(state);
    sendIMessage(okSubmit ? `QBO timecard submitted: ${finalPlan.total}h for ${finalPlan.week_start} to ${finalPlan.week_end}.` : `QBO timecard submit ran (${picked} days), readback needs review for ${finalPlan.period}.`);
    console.log(okSubmit ? 'submitted and readback matched' : 'submit clicked; readback verify needed');
  } catch (e) {
    if (e.code === 'NO_CDP' && has('--scheduled')) {
      maybeAlert(state, 'no-cdp', 'QuickBooks timecard Chrome is not reachable. Run: bash scripts/login_capture_quickbooks.sh, sign in, open the timesheet, and leave it open.', 'once');
      saveState(state);
    }
    console.error('error: ' + e.message);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}

(async () => {
  const state = loadState();
  if (has('--map')) {
    await mapFlow(state);
    return;
  }
  await runDriver(state);
})();
