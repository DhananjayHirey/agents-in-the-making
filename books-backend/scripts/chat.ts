/**
 * Terminal chat client — the same calls the web page makes, for quick testing.
 *
 *   npm run chat -- --user editor@books.local --password <from seed-users.ts>
 *   npm run chat -- --user admin@books.local --password <...> --say "Delete Clean Code" --auto-confirm
 *
 * --say  sends one message and exits (repeatable); otherwise it is interactive.
 * --auto-confirm  approves every confirm card (handy for scripted runs).
 */
import readline from 'node:readline/promises';
import { randomUUID } from 'node:crypto';
import { stdin as input, stdout as output } from 'node:process';
import 'dotenv/config';

const BASE = `http://localhost:${process.env.PORT ?? 4000}`;
const args = process.argv.slice(2);
const flag = (name: string) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : undefined;
};
const says = args.flatMap((a, i) => (a === '--say' ? [args[i + 1]] : []));
const autoConfirm = args.includes('--auto-confirm');

let token = '';
let conversationId = '';
const pendingConfirms: Array<{ actionId: string; summary: string }> = [];

async function call(path: string, body?: unknown, method = 'POST') {
  return fetch(`${BASE}${path}`, {
    method,
    headers: { 'content-type': 'application/json', authorization: token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function readStream(res: Response) {
  if (!res.ok || !(res.headers.get('content-type') ?? '').includes('text/event-stream')) {
    console.log(`  ! HTTP ${res.status}: ${await res.text()}`);
    return;
  }
  const decoder = new TextDecoder();
  let buf = '';
  let streaming = false;
  for await (const chunk of res.body as any as AsyncIterable<Uint8Array>) {
    buf += decoder.decode(chunk, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (frame.startsWith(':')) continue;
      let seq = '',
        type = '',
        data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('id: ')) seq = line.slice(4);
        else if (line.startsWith('event: ')) type = line.slice(7);
        else if (line.startsWith('data: ')) data += line.slice(6);
      }
      const d = data ? JSON.parse(data) : {};
      const tag = `[${seq.padStart(3)}] ${type}`;
      if (type === 'content_delta') {
        if (!streaming) process.stdout.write(`${tag}: `);
        streaming = true;
        process.stdout.write(d.text);
        continue;
      }
      if (streaming) {
        process.stdout.write('\n');
        streaming = false;
      }
      if (type === 'confirm_required') {
        pendingConfirms.push({ actionId: d.actionId, summary: d.preview.summary });
        console.log(
          `${tag}: ${d.preview.summary}\n        before=${JSON.stringify(d.preview.before)}\n        after=${JSON.stringify(d.preview.after)}`,
        );
      } else if (type === 'assistant_message') {
        console.log(`${tag}: ${d.text}`);
      } else {
        const { turnId, ...rest } = d;
        console.log(`${tag} ${JSON.stringify(rest).slice(0, 220)}`);
      }
    }
  }
}

async function send(text: string, rl?: readline.Interface) {
  if (!rl) console.log(`\nyou> ${text}`);
  await readStream(
    await call(`/api/agent/conversations/${conversationId}/messages`, { turnId: `t_${randomUUID()}`, text }),
  );
  while (pendingConfirms.length) {
    const p = pendingConfirms.shift()!;
    let decision = 'approve';
    if (!autoConfirm && rl) {
      const answer = await rl.question(`Confirm "${p.summary}"? [y/N] `);
      decision = answer.trim().toLowerCase().startsWith('y') ? 'approve' : 'reject';
    } else if (!autoConfirm) {
      decision = 'reject';
      console.log(`(scripted run without --auto-confirm) rejecting "${p.summary}"`);
    } else {
      console.log(`(auto-confirm) approving "${p.summary}"`);
    }
    await readStream(
      await call(`/api/agent/conversations/${conversationId}/confirm`, {
        turnId: `t_${randomUUID()}`,
        actionId: p.actionId,
        decision,
      }),
    );
  }
}

async function main() {
  const login = await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ email: flag('user'), password: flag('password') }),
  });
  const body = await login.json();
  if (!login.ok) throw new Error(body.error);
  token = body.token;
  console.log(`signed in as ${body.user.name} (${body.user.role})`);

  const conv = await (await call('/api/agent/conversations', {})).json();
  conversationId = conv.conversationId;
  console.log(`conversation ${conversationId}`);

  if (says.length) {
    for (const s of says) await send(s);
    return;
  }
  const rl = readline.createInterface({ input, output });
  for (;;) {
    const text = (await rl.question('\nyou> ')).trim();
    if (!text || text === 'exit') break;
    await send(text, rl);
  }
  rl.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
