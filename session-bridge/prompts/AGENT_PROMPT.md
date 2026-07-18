# session-bridge — instructions for your CLI agent

Give this to whatever coding/CLI agent you run (add it to the agent's project
instructions, e.g. its `CLAUDE.md` / `AGENTS.md`, or paste it at the start of a
session). It teaches the agent to hand a finished message off through the
bridge instead of trying to operate your email/chat systems directly.

## What you do

When the operator asks you to draft or reply to someone, you do NOT open their
email or chat client. You write the message and drop a **job file** into the
bridge queue. A worker on the machine that holds the real integration stages
the draft and writes a result back. You never send — you stage; the operator
sends.

Queue inbox (set this to the operator's configured `queue` path + `/inbox`):

    <QUEUE>/inbox/<id>.job.json

Results come back at `<QUEUE>/outbox/<id>.result.json`.

## Write in the operator's voice

The `body` you write is what gets staged. Match the operator's house style in
`prompts/STYLE.md` (they fill it in). Do not invent a signature or sign-off —
the connector appends whatever signature belongs. Do not paste raw links;
describe them and let the connector/operator handle formatting.

## Job format

Write `<QUEUE>/inbox/<id>.job.json`. `id` is any unique, descriptive slug —
make it up from the context, don't rely on randomness.

    {
      "id": "acct-rebate-reply",
      "channel": "email",
      "action": "reply",
      "thread": "sender name + a few subject words to locate the thread",
      "cc": "",
      "body": "the message, in the operator's voice",
      "origin": "cli-session"
    }

- `channel` — a channel the operator configured (e.g. `email`, `chat`).
- `action` — `reply` (into an existing thread) or `new`.
- `thread` — for replies: enough text to identify the thread.
- `to` / `subject` — for new messages.
- `cc` — optional.
- `body` — required, in the operator's voice.
- `origin` — free label so the result is traceable to this session.

Drop it with a heredoc:

    cat > "<QUEUE>/inbox/acct-rebate-reply.job.json" <<'JSON'
    { ...the job... }
    JSON

## Confirm it landed

After ~a poll interval (plus any folder-sync lag), read the result:

    cat "<QUEUE>/outbox/acct-rebate-reply.result.json"

- `status: "staged"` — the draft is ready in the operator's system for review.
- `status: "error"` — read `error`/`trace`, fix the job, re-drop with a new id.

Never auto-send. Stage, report the result, stop.
