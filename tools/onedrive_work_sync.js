#!/usr/bin/env node
/* Keeps my work-reference notes synced to OneDrive so they're current on all my devices.
 * Copies the latest work notes (inbox, calendar, meetings, team chat, sprint, DPP, portal,
 * Tavant, people) into the OneDrive "Work Reference" folder; OneDrive handles the sync.
 * Writes clean, readable copies with consistent titles (no scratch headers). */
const fs = require('fs'), path = require('path'), os = require('os'), crypto = require('crypto');
const SRC = path.join(os.homedir(), '.openclaw/workspace/CompanyA-local/digests');
const DEST_ROOT = process.env.ONEDRIVE_redacted_DIR
  || path.join(os.homedir(), 'Library/CloudStorage/OneDrive-redactedIndustries,Inc');
const DEST = path.join(DEST_ROOT, process.env.ONEDRIVE_FOLDER || 'Work Reference');

// source note -> { out: clean filename, title: clean H1 }
const FILES = [
  { src: 'SFDC_REFERENCE.md',         out: 'SFDC Reference.md',    title: '# SFDC Reference — start here for build + ticket work' },
  { src: 'SF_BUILD_LOG.md',           out: 'Build Log.md',         title: '# Salesforce — Build Log (commits + open PRs)' },
  { src: 'SF_SPRINT_BOARD.md',        out: 'Salesforce Tickets.md', title: '# Salesforce Tickets — Sprint Board' },
  { src: 'ZOOM_CALENDAR.md',          out: 'Calendar.md',          title: '# Calendar' },
  { src: 'ZOOM_MEETING_SUMMARIES.md', out: 'Meeting Summaries.md', title: '# Meeting Summaries' },
  { src: 'ZOOM_TEAM_CHAT.md',         out: 'Team Chat.md',         title: '# Team Chat' },
  { src: 'LITTLEBIRD_NOTES.md',       out: 'Meeting Notes.md',     title: '# Meeting Notes' },
  { src: 'dpp_program.md',            out: 'DPP Program.md',       title: '# DPP — Dealer Performance Program' },
  { src: 'sp_portal.md',              out: 'Partner Portal.md',    title: '# Partner / Service Portal' },
  { src: 'sprint_current.md',         out: 'Current Sprint.md',    title: '# Current Sprint' },
  { src: 'tavant.md',                 out: 'Tavant.md',            title: '# Tavant — Warranty / Commissioning Platform' },
  { src: 'people_map.md',             out: 'People.md',            title: '# People — who\'s who' },
  { src: 'writing_voice.md',          out: 'How I Write.md',       title: '# How I Write' },
  { src: 'WEEKLY_DIGEST.md',          out: 'Weekly Digest.md',     title: '# Weekly Work Digest' },
  { src: 'WORK_PARITY.md',            out: 'Work Parity.md',       title: '# Work Parity' },
  { src: 'HANDOFF_fern_sp_portal_2026-07-14.md', out: 'SP Portal Handoff.md', title: '# SP Portal Handoff' },
  { src: 'HANDOFF_prepay_and_sp_portal_2026-07-14.md', out: 'Prepay and Portal Handoff.md', title: '# Prepay and Portal Handoff' },
];

const RETRY_CODES = new Set(['EAGAIN', 'EBUSY', 'ETIMEDOUT']);
const pause = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
function writeManaged(target, text, managed, io = fs, attempts = 4) {
  const resolved = path.resolve(target);
  if (!managed.has(resolved)) throw Object.assign(new Error('refusing unmanaged OneDrive target'), { code: 'EPERM' });
  let last;
  for (let i = 0; i < attempts; i++) {
    const temp = path.join(path.dirname(resolved), `.${path.basename(resolved)}.sync-${process.pid}-${Date.now()}-${i}`);
    try {
      io.mkdirSync(path.dirname(resolved), { recursive: true });
      io.writeFileSync(temp, text);
      io.renameSync(temp, resolved);
      return;
    } catch (e) {
      last = e;
      try { io.rmSync(temp, { force: true }); } catch {}
      if (!RETRY_CODES.has(e.code)) throw e;
      if (i + 1 < attempts) pause(100 * (i + 1));
    }
  }
  // A stale File Provider placeholder can reject replacement while sibling files work.
  // Removing is allowed only after the exact target passed the managed-set check above.
  const temp = path.join(path.dirname(resolved), `.${path.basename(resolved)}.replace-${process.pid}-${Date.now()}`);
  try {
    io.writeFileSync(temp, text);
    io.rmSync(resolved, { force: true });
    io.renameSync(temp, resolved);
  } catch (e) {
    try { io.rmSync(temp, { force: true }); } catch {}
    throw e || last;
  }
}
function removeManaged(target, managed, io = fs, attempts = 4) {
  const resolved = path.resolve(target);
  if (!managed.has(resolved)) throw Object.assign(new Error('refusing unmanaged OneDrive target'), { code: 'EPERM' });
  for (let i = 0; i < attempts; i++) {
    try { io.rmSync(resolved, { force: true }); return; }
    catch (e) {
      if (!RETRY_CODES.has(e.code) || i + 1 >= attempts) throw e;
      pause(100 * (i + 1));
    }
  }
}

const digest = text => crypto.createHash('sha256').update(text).digest('hex');

// Drop personal (non-work) meeting blocks so only sanitized WORK content reaches the employer surface.
// A block runs from one ## / ### heading to the next; remove it if its source marker is a personal
// FaceTime call (com.apple.avconferenced) or its text hits the personal denylist (meds, family, camp...).
const PERSONAL_RE = /\b(medication|\bmeds\b|the goat|day ?camp|ymca|family check-?in|airport call|school drop-?off|pick-?up the kids|personal call|doctor'?s? appointment)\b/i;
function stripPersonalBlocks(body) {
  const lines = body.split('\n');
  const out = [];
  let block = [], isHeading = l => /^#{2,3}\s/.test(l);
  const flush = () => {
    if (!block.length) return;
    const head = block[0] || '';
    const text = block.join('\n');
    const personal = /com\.apple\.avconferenced/i.test(head) || PERSONAL_RE.test(text);
    if (!personal) out.push(...block);
    block = [];
  };
  for (const l of lines) { if (isHeading(l)) flush(); block.push(l); }
  flush();
  return out.join('\n');
}

// normalize to a clean, readable note: set a consistent title, drop scratch/timestamp header lines
function clean(raw, title) {
  let lines = raw.split('\n').filter(l =>
    !/^_generated/i.test(l) &&
    !/^AUTO-REFRESHED/i.test(l) &&
    !/^Generated:/i.test(l) &&
    !/web bridge/i.test(l) &&
    !/web\/IDB bridge/i.test(l));
  const h1 = lines.findIndex(l => /^#\s/.test(l));
  if (h1 !== -1) lines.splice(h1, 1);
  let body = stripPersonalBlocks(lines.join('\n'))
    .replace(/file:\/\/\/[^\s)`'"]*/g, '')
    .replace(/\/Users\/[A-Za-z0-9._-]+\/[^\s)`'"]*/g, '')
    .replace(/com\.claude-stack[.\w-]*/gi, '')
    .replace(/claude[-\s]?stack/gi, '')
    .replace(/openclaw/gi, '')
    .replace(/hailports/gi, '')
    .replace(/\bcodex\b/gi, '')
    .replace(/co-?authored-?by/gi, '')
    .replace(/\bsub-?agents?\b/gi, '')
    .replace(/\bChatGPT\b/gi, 'my notes')
    .replace(/\bclaude(['’ʼ´`]s|s)?\b/gi, 'my')
    // scrub any pointer to the personal AI tooling/stack (keeps sanctioned tools like Cursor/Copilot intact)
    .replace(/\bAI[-\s]?stack\b/gi, 'tooling')
    .replace(/\b(my|the|his)\s+AI\s+(stack|tooling|agents?|assistant|setup)\b/gi, 'my tooling')
    .replace(/\bAI[-\s]?driven\b/gi, 'streamlined')
    .replace(/\bAI[-\s]?(powered|assisted|generated)\b/gi, 'automated')
    .replace(/\bAI development integration\b/gi, 'development tooling')
    .replace(/\b(anthropic|ollama|claude\s*code|claude\s*opus|sonnet|\bllm\b|gpt-?\d[\w.]*|subagents?|autonomous agents?)\b/gi, '')
    .replace(/\bus\.zoom\.xos\b|\bcom\.microsoft\.teams2\b|\bcom\.apple\.WebKit\.GPU\b/g, '')
    // device identity: the mini's own name must never reach the employer surface
    // (OneDrive conflict-renames + digest leakage). Map to a neutral label.
    .replace(/Mac[\s-]*mini/gi, 'this device')
    .replace(new RegExp('\\b' + (os.hostname().split('.')[0] || 'zzzz').replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'gi'), 'this device')
    .replace(/[ \t]+·[ \t]*$/gm, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n');
  return `${title}\n\n${body.replace(/^\n+/, '')}`.replace(/\s+$/, '') + '\n';
}

function run() {
// OWNERSHIP GUARD (hard, fail-closed): only EVER write to Operator's OWN OneDrive personal space.
// Refuse if the destination is a SharePoint shared library, a /sites/ path, or anything not directly
// under his own OneDrive root — even if env vars try to redirect it. We never write to non-owned storage.
const OWN_ROOT = path.resolve(path.join(os.homedir(), 'Library/CloudStorage/OneDrive-redactedIndustries,Inc'));
const rDestRoot = path.resolve(DEST_ROOT);
const rDest = path.resolve(DEST);
if (rDestRoot !== OWN_ROOT
    || /SharedLibraries|sharepoint|\/sites\//i.test(DEST_ROOT + ' ' + DEST)
    || rDest.indexOf(OWN_ROOT + path.sep) !== 0) {
  console.error('[work-sync] REFUSED: destination is not Operator\'s own OneDrive space — never write to non-owned storage.');
  process.exit(9);
}

if (!fs.existsSync(DEST_ROOT)) {
  console.error(`[work-sync] OneDrive folder not found: ${DEST_ROOT} — is OneDrive signed in?`);
  process.exit(2);
}
fs.mkdirSync(DEST, { recursive: true });
const INDEX_NAME = 'Start Here.md';
const keep = new Set(FILES.map(f => f.out));
keep.add(INDEX_NAME);
keep.add('Brain Status.md');
keep.add('Current Work Mail.md');
keep.add('Topic Reference.md');
keep.add('Durable Work Reference.md');
keep.add('Projects');
// coordination blackboard — written by agents on any device, not sourced from a digest; never delete
keep.add('Working On Now.md');
keep.add('Activity Log.md');
keep.add('_coordination');
keep.add('_setup');
// Targeted cleanup: only remove OneDrive conflict-copies of OUR managed files
// ("<managed>-<device>.<ext>", e.g. "Working On Now-Mac mini.md") + anything whose
// name carries the mini's device identity. Per-file try/catch so one un-removable
// cloud placeholder can't abort the sweep. We NEVER blanket-delete unknown files —
// the user parks real folders/docs here (Customer Domain, etc.); leave them alone.
const HOSTBASE = os.hostname().split('.')[0];
const DEVICE_RE = new RegExp('mac[-\\s]?mini' + (HOSTBASE ? '|' + HOSTBASE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') : ''), 'i');
const isOurConflictCopy = n => {
  const m = n.match(/^(.+?)-[A-Za-z0-9 .-]+(\.[A-Za-z0-9]+)$/);
  return m && keep.has(m[1] + m[2]);
};
try {
  for (const f of fs.readdirSync(DEST)) {
    if (keep.has(f)) continue;
    if (DEVICE_RE.test(f) || isOurConflictCopy(f)) {
      try { fs.rmSync(path.join(DEST, f), { force: true }); }
      catch (e) { console.error(`[work-sync] could not remove conflict copy ${f}: ${e.message}`); }
    }
  }
} catch (e) { console.error(`[work-sync] cleanup scan failed: ${e.message}`); }

const managed = new Set([...keep].filter(n => !['_coordination', '_setup', 'Projects'].includes(n))
  .map(n => path.resolve(path.join(DEST, n))));
const markerPath = path.join(SRC, '.work_notes_sync.json');
let previous = {};
try { previous = JSON.parse(fs.readFileSync(markerPath, 'utf8')); } catch {}
const hashes = Object.assign({}, previous.hashes || {});
let ok = 0, skip = 0, fail = 0, unchanged = 0;
const retired = ['Outlook Inbox.md', 'Outlook Sent.md'];
const retiredManaged = new Set(retired.map(n => path.resolve(path.join(DEST, n))));
for (const out of retired) {
  const target = path.join(DEST, out);
  if (!fs.existsSync(target)) continue;
  try { removeManaged(target, retiredManaged); ok++; delete hashes[out]; }
  catch (e) { fail++; console.error(`[work-sync] retired ${out} failed: ${e.message}`); }
}
for (const { src, out, title } of FILES) {
  const sp = path.join(SRC, src);
  if (!fs.existsSync(sp)) { skip++; console.error(`[work-sync] ${src} missing`); continue; }
  try {
    const text = clean(fs.readFileSync(sp, 'utf8'), title);
    const dp = path.join(DEST, out);
    const hash = digest(text);
    if (previous.failed === 0 && previous.missing === 0 && hashes[out] === hash && fs.existsSync(dp)) {
      unchanged++; continue;
    }
    writeManaged(dp, text, managed);
    hashes[out] = hash;
    ok++;
  } catch (e) { fail++; console.error(`[work-sync] ${src} failed: ${e.message}`); }
}
// orientation index — reads as a normal knowledge-base contents page (present-tense, no tooling framing)
const DESC = {
  'SFDC Reference.md': 'START HERE for Salesforce build/ticket work — points to the build log, tickets + activity log',
  'Build Log.md': 'every commit (newest first) + open pull requests — what has been built + deployed',
  'Salesforce Tickets.md': 'open + backlog sprint-board tickets — status, owner, epic, sprint, my notes + the latest comments',
  'Calendar.md': 'upcoming + recent meetings',
  'Meeting Summaries.md': 'meeting recaps',
  'Meeting Notes.md': 'my meeting notes',
  'Team Chat.md': 'Zoom team channels + threads',
  'Current Sprint.md': 'current sprint focus',
  'DPP Program.md': 'dealer performance program',
  'Partner Portal.md': 'partner / service portal',
  'Tavant.md': 'warranty / commissioning platform',
  'People.md': "who's who",
  'Weekly Digest.md': 'weekly work summary, open decisions + action items',
  'Work Parity.md': 'cross-device work-reference freshness + parity',
  'SP Portal Handoff.md': 'current SP portal recovery context',
  'Prepay and Portal Handoff.md': 'current prepay + portal recovery context',
};
try {
  const present = FILES.filter(f => fs.existsSync(path.join(SRC, f.src)));
  const lines = present.map(f => `- **${f.out}** — ${DESC[f.out] || ''}`.replace(/ — $/, ''));
  const idx = `# Start Here — Work Reference\n\n`
    + `my working notes for the Valley / Salesforce work, kept current here so they're the same on every device.\n\n`
    + `## what's in here\n${lines.join('\n')}\n`
    + `- **Projects/Index.md** — current logical project references\n`
    + `- **Current Work Mail.md** — current WCI-backed mail reference\n`
    + `- **Topic Reference.md** — current topic coverage\n`
    + `- **Durable Work Reference.md** — sanitized durable decisions + context\n`
    + `- **Brain Status.md** — coverage, counts + freshness for the work brain\n`;
  const indexHash = digest(idx);
  if (!(previous.failed === 0 && previous.missing === 0 && hashes[INDEX_NAME] === indexHash
        && fs.existsSync(path.join(DEST, INDEX_NAME)))) {
    writeManaged(path.join(DEST, INDEX_NAME), idx, managed);
    hashes[INDEX_NAME] = indexHash;
    ok++;
  } else unchanged++;
} catch (e) { fail++; console.error(`[work-sync] index failed: ${e.message}`); }

const ts = new Date().toISOString();
try { fs.writeFileSync(markerPath,
  JSON.stringify({ attempted: ts, notes_synced: fail === 0 && skip === 0 ? ts : previous.notes_synced || null,
    updated: ok, unchanged, missing: skip, failed: fail, hashes }, null, 2)); } catch {}
console.log(`[work-sync] ${ts} -> ${path.basename(DEST)}: ${ok} updated, ${unchanged} unchanged, ${skip} missing, ${fail} failed`);
return fail || skip ? 5 : 0;
}

if (require.main === module) process.exit(run());

module.exports = { FILES, clean, stripPersonalBlocks, writeManaged, removeManaged, run };
