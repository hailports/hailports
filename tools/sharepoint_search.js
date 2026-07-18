#!/usr/bin/env node
/* sharepoint_search — on-demand search across all of my CompanyA SharePoint (everything I have
 * access to, tenant-wide), using my own signed-in session. Runs ONLY when I ask; nothing scheduled.
 * Uses the SharePoint Search REST API (same search the SharePoint website uses) via my existing
 * browser session, so results respect exactly my own permissions.
 *
 * READ-ONLY INVARIANT (hard rule): this tool ONLY issues GET search queries. It must NEVER write,
 * edit, upload, comment on, or delete anything in SharePoint — we don't own it. Discovery only.
 *
 *   node tools/sharepoint_search.js "quote process repipe"
 *   node tools/sharepoint_search.js "dealer onboarding" --rows 15
 *   node tools/sharepoint_search.js "tavant migration" --site IrrigationSalesforceProjects
 */
const { chromium } = require('/home/user/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const CDP = process.env.OWA_WEB_CDP || 'http://127.0.0.1:18820';
const TENANT = process.env.SP_TENANT || 'https://CompanyA.sharepoint.com';

const args = process.argv.slice(2);
const rows = (() => { const i = args.indexOf('--rows'); return i >= 0 ? Number(args[i + 1]) : 20; })();
const site = (() => { const i = args.indexOf('--site'); return i >= 0 ? args[i + 1] : null; })();
let query = args.filter((a, i) => !a.startsWith('--') && args[i - 1] !== '--rows' && args[i - 1] !== '--site').join(' ').trim();
if (!query) { console.error('usage: sharepoint_search.js "<query>" [--rows N] [--site <SiteName>]'); process.exit(1); }
if (site) query += ` SiteName:${site}`;

const cell = (row, key) => { const c = (row.Cells || []).find(c => c.Key === key); return c ? c.Value : ''; };
const fmt = s => { try { return new Date(s).toISOString().slice(0, 10); } catch { return s || ''; } };

(async () => {
  let b;
  try { b = await chromium.connectOverCDP(CDP); }
  catch (e) {
    try { require('child_process').execSync('bash ' + require('path').join(__dirname, '../scripts/owa_web_sync.sh'), { timeout: 60000, stdio: 'ignore' }); } catch (e2) {}
    await new Promise(r => setTimeout(r, 14000));
    try { b = await chromium.connectOverCDP(CDP); }
    catch (e3) { console.error(`[sp-search] cannot connect ${CDP} — bring up the signed-in Outlook profile first. ${e3.message}`); process.exit(2); }
  }
  const ctx = b.contexts()[0];
  const pg = await ctx.newPage();
  try {
    await pg.goto(TENANT + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
  } catch (e) { console.error('[sp-search] could not reach SharePoint:', e.message); await b.close(); process.exit(3); }
  if (/login|signin/i.test(pg.url())) {
    console.error('[sp-search] SharePoint not signed in — re-login the Outlook/OWA profile once, then retry.');
    await pg.close(); await b.close(); process.exit(4);
  }
  const props = 'Title,Path,Author,LastModifiedTime,HitHighlightedSummary,FileType,SiteName';
  const result = await pg.evaluate(async ({ q, rows, props }) => {
    const u = `/_api/search/query?querytext='${encodeURIComponent(q)}'&rowlimit=${rows}`
      + `&selectproperties='${encodeURIComponent(props)}'&clienttype='ContentSearchRegular'`;
    const res = await fetch(u, { headers: { Accept: 'application/json;odata=nometadata' } });
    if (res.status !== 200) return { status: res.status, text: (await res.text()).slice(0, 300) };
    const j = await res.json();
    const rel = j.PrimaryQueryResult && j.PrimaryQueryResult.RelevantResults;
    const total = rel ? rel.TotalRows : 0;
    const rrows = (rel && rel.Table && rel.Table.Rows) || [];
    return { status: 200, total, rows: rrows };
  }, { q: query, rows, props });
  await pg.close(); await b.close();

  if (result.status !== 200) { console.error('[sp-search] search failed', result.status, result.text || ''); process.exit(5); }
  const out = (result.rows || []).map(r => ({
    title: cell(r, 'Title') || '(untitled)',
    path: cell(r, 'Path'),
    author: cell(r, 'Author'),
    modified: fmt(cell(r, 'LastModifiedTime')),
    site: cell(r, 'SiteName'),
    type: cell(r, 'FileType'),
    summary: (cell(r, 'HitHighlightedSummary') || '').replace(/<\/?c0>|<ddd\/>/g, '').replace(/\s+/g, ' ').trim(),
  }));
  console.log(`# SharePoint: "${query}" — ${out.length} of ~${result.total} results\n`);
  for (const r of out) {
    console.log(`### ${r.title}${r.type ? ' · ' + r.type : ''}`);
    console.log(`- ${r.modified}${r.author ? ' · ' + r.author : ''}${r.site ? ' · ' + r.site : ''}`);
    if (r.summary) console.log(`- ${r.summary}`);
    if (r.path) console.log(`- ${r.path}`);
    console.log('');
  }
})();
