// mini-pulse — OFF-BOX dead-man's monitor for the Mac mini.
//
// The mini pings /ping every ~2min. This Worker runs on Cloudflare (NOT the mini),
// so if the mini goes fully dark — power loss, hung on boot, network down, wedged
// reboot — the scheduled handler notices the pings stopped and emails Operator. This is
// the ONE failure every on-box guard is blind to: a dead box can't report itself dead.
//
// Bindings: KV "PULSE". Secrets: PING_SECRET, RESEND_API_KEY, ALERT_EMAIL, FROM_EMAIL.

const THRESHOLD_MS = 12 * 60 * 1000; // >12min with no ping = the box is dark

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const k = url.searchParams.get("k");

    if (url.pathname === "/ping") {
      if (k !== env.PING_SECRET) return new Response("no", { status: 403 });
      await env.PULSE.put("last_ping", String(Date.now()));
      await env.PULSE.put("alerted", "0"); // fresh ping clears any dark state
      return new Response("ok");
    }

    if (url.pathname === "/status") {
      const last = Number(await env.PULSE.get("last_ping")) || 0;
      const age = Date.now() - last;
      return Response.json({
        last_ping: last,
        age_ms: age,
        age_min: last ? Math.round(age / 60000) : null,
        dark: last > 0 && age > THRESHOLD_MS,
        alerted: (await env.PULSE.get("alerted")) === "1",
      });
    }

    // Proof route — forces the exact dark-box email so delivery is verifiable on demand.
    if (url.pathname === "/test-alert") {
      if (k !== env.PING_SECRET) return new Response("no", { status: 403 });
      const id = await sendAlert(env, "TEST", true);
      return new Response("test alert sent id=" + id);
    }

    return new Response("mini-pulse", { status: 200 });
  },

  async scheduled(event, env, ctx) {
    const last = Number(await env.PULSE.get("last_ping")) || 0;
    if (last === 0) return; // never seen a ping yet — don't alert during first-deploy window
    const alerted = (await env.PULSE.get("alerted")) === "1";
    const age = Date.now() - last;
    if (age > THRESHOLD_MS && !alerted) {
      await sendAlert(env, Math.round(age / 60000), false);
      await env.PULSE.put("alerted", "1"); // one alert per outage, not a stream
    }
  },
};

async function sendAlert(env, mins, isTest) {
  const subject = isTest
    ? "mini-pulse TEST — this is what a dark-box alert looks like"
    : `mini is DARK — no heartbeat in ${mins}min`;
  const lead = isTest
    ? "this is a TEST of the off-box monitor. if you're reading it, the dead-box alert path works end to end."
    : `the mac mini has stopped pinging the off-box monitor for ${mins} minutes.`;
  const text =
    `${lead}\n\n` +
    `this alert comes from cloudflare, not the mini -- so it fires even when the box is fully down, hung, or offline. ` +
    `every on-box self-heal guard is blind to this case (a dark box can't fix itself).\n\n` +
    `likely causes: power loss, hung on boot, network down, or a wedged reboot.\n\n` +
    `what to do: confirm the mini has power + network, then try tailscale/ssh. if it reboots clean, the heartbeat ` +
    `resumes on its own and this clears automatically -- no action needed once it's back.`;
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.RESEND_API_KEY}`,
    },
    body: JSON.stringify({
      from: `mini pulse <${env.FROM_EMAIL}>`,
      to: [env.ALERT_EMAIL],
      subject,
      text,
    }),
  });
  const j = await r.json().catch(() => ({}));
  return j.id || ("http" + r.status);
}
