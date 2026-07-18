# session-bridge

Hand work off from a command-line agent to the systems you actually use —
without the agent ever driving those systems directly.

You run long, intense sessions in the terminal with a coding/CLI agent. Some of
what comes out belongs in a real system: an email draft, a chat message, a
ticket, a calendar hold. Often the agent can't reach that system — it lives on
another machine, behind a login, or only speaks a native desktop API. Even when
it can, you rarely want an agent clicking around in your inbox.

session-bridge decouples the two with a tiny, file-based queue:

```
  CLI agent  ──drops──▶  <queue>/inbox/<id>.job.json
                              │   (any shared / synced directory)
   worker    ──runs───▶  connector command  ──stages──▶  your system
                              │
             ──writes─▶  <queue>/outbox/<id>.result.json
```

- **Transport is just a shared folder.** Cloud-synced folder, NFS/SMB mount,
  shared git working tree — anything both sides can see. No server, no ports.
- **Connectors are just commands.** A connector reads the job JSON on stdin,
  stages a draft in your system, and prints a JSON result on stdout. So a
  "channel" can be anything you can script. The core knows nothing about any
  specific email client, chat app, or provider.
- **Stage, don't send.** The bridge produces reviewable drafts. A human sends.

## Quick start

```sh
cp config.example.toml config.toml      # set queue path + your connectors
python3 bridge.py --config config.toml  # start the worker (keep it running)
```

Then, from any agent session, drop a job:

```sh
Q=~/bridge-queue        # your configured queue
cat > "$Q/inbox/hello.job.json" <<'JSON'
{"id":"hello","channel":"email","to":"someone@example.com",
 "subject":"quick note","body":"testing the bridge","origin":"cli-session"}
JSON
```

Within a poll interval the worker stages it and writes
`$Q/outbox/hello.result.json`. The bundled `examples/stage_email.py` /
`examples/stage_chat.py` "stage" to a local drafts folder so you can watch the
whole loop with zero external accounts — then replace their `stage()` with a
call into your own system.

## Wiring your own systems

A connector is any command that:

1. reads one job JSON object on **stdin**,
2. stages it however it likes (API call, local scripting bridge, save-to-drafts),
3. prints a JSON result object on **stdout**, and exits `0` on success.

Map channels to commands in `config.toml`:

```toml
[connectors]
email = "python3 ./examples/stage_email.py --drafts ~/bridge-drafts"
chat  = "python3 ./my-connectors/team_chat.py"
ticket = "node ./my-connectors/tickets.js"
```

Because the worker runs on the machine that owns the integration, the agent can
be anywhere — a different laptop, a container, a remote box — as long as it can
write into the shared queue.

## Teaching the agent

`prompts/AGENT_PROMPT.md` is a drop-in instruction block that teaches a CLI
agent to write in your voice and hand off via the queue instead of operating
your systems. `prompts/STYLE.example.md` is the voice/style file it reads —
copy it to `STYLE.md` and make it yours.

## Files

| path | what it is |
|---|---|
| `bridge.py` | the worker: watch the queue, dispatch jobs to connectors, write results |
| `config.example.toml` | queue path, poll interval, channel → connector command map |
| `schema/job.schema.json` | the job contract |
| `examples/stage_email.py`, `examples/stage_chat.py` | reference connectors that stage to a local folder |
| `prompts/AGENT_PROMPT.md` | instructions for your CLI agent |
| `prompts/STYLE.example.md` | your house-style template |

## Notes

- Jobs move to `processed/` or `failed/` after handling; results always land in
  `outbox/`. Nothing is deleted.
- Run the worker under whatever keeps a process alive on your platform
  (a service manager, a cron `--once` loop, a terminal multiplexer).
- The worker gives each connector a bounded run and captures its output, so a
  hung or failing connector fails that one job instead of the queue.
```
