/* OWA re-auth window manager — used by scripts/owa_web_sync.sh.
 *   node tools/owa_window.js show   → surface the OWA Chrome window on-screen (for the one human
 *                                     step: the MFA/device-auth tap), navigate to mail to trigger
 *                                     the sign-in, and auto-accept "Stay signed in?" (KMSI is not a
 *                                     security gate — it just lengthens the persistent cookie).
 *                                     Idempotent: if already on-screen it won't re-steal focus,
 *                                     it only keeps nudging the KMSI button (post-MFA).
 *   node tools/owa_window.js hide   → minimize the window out of view. The bridge still reads over
 *                                     CDP (it force-reloads each cycle), and on a single display a
 *                                     runtime off-screen move only clamps to ~-760 (still visible),
 *                                     so minimize is the reliable hide.
 * Never types a password or completes MFA — those stay human. */
const PW = process.env.HOME + "/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core";
const { chromium } = require(PW);
const PORT = process.env.OWA_CDP_PORT || "18820";
const mode = (process.argv[2] || "hide").toLowerCase();

(async () => {
  const b = await chromium.connectOverCDP("http://127.0.0.1:" + PORT);
  const ctx = b.contexts()[0];
  if (!ctx) { console.log("owa_window: no context"); await b.close(); return; }
  let pg = ctx.pages().find(p => /outlook|office|microsoft|login|adfs/.test(p.url() || "")) || ctx.pages()[0];
  if (!pg) { console.log("owa_window: no page"); await b.close(); return; }
  const s = await ctx.newCDPSession(pg);
  const { windowId } = await s.send("Browser.getWindowForTarget");

  if (mode === "hide") {
    await s.send("Browser.setWindowBounds", { windowId, bounds: { windowState: "minimized" } });
    console.log("owa_window: hidden (minimized)");
    await s.detach(); await b.close(); return;
  }

  // show
  const { bounds } = await s.send("Browser.getWindowBounds", { windowId });
  const alreadyShown = bounds.windowState === "normal" && (bounds.left || 0) > -5000;
  if (!alreadyShown) {
    await s.send("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal", left: 80, top: 80, width: 1400, height: 1000 } });
    if (!/outlook\.(office\.com|cloud\.microsoft)\/mail/.test(pg.url() || "")) {
      try { await pg.goto("https://outlook.office.com/mail/", { waitUntil: "domcontentloaded", timeout: 25000 }); } catch (e) {}
    }
    try { await pg.bringToFront(); } catch (e) {}
  }
  // opportunistic KMSI accept (safe: only advances the standard MS flow; no creds)
  for (let i = 0; i < 8; i++) {
    try {
      const yes = await pg.$('#idSIButton9, input[type=submit][value="Yes"]');
      if (yes) await yes.click({ timeout: 1500 }).catch(() => {});
    } catch (e) {}
    await pg.waitForTimeout(1000);
  }
  console.log("owa_window: shown url=" + (pg.url() || "").slice(0, 70));
  await s.detach(); await b.close();
})().catch(e => { console.log("owa_window ERR " + String(e).slice(0, 160)); process.exit(0); });
