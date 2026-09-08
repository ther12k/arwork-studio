/**
 * Color Duel Art Studio — AI bridge (port 8787)
 *
 * A private, server-to-server OpenAI-compatible shim backed by z-ai-web-dev-sdk.
 * The Python studio's Provider (studio/ai.py) points AI_BASE_URL at this service:
 *   POST /v1/responses            -> art-direction chat (brief refinement, JSON {reply, brief})
 *   POST /v1/json                  -> generic strict-JSON extraction (scene planner)
 *   POST /v1/svg                   -> separate SVG-generation route (model authors the master)
 *   POST /v1/images/generations   -> master image generation from the brief
 *   POST /v1/images/edits         -> owned-image / current-master editing (multipart)
 *   GET  /healthz                 -> liveness
 *
 * Only the local Python service talks to it; it binds 127.0.0.1 and never exposes keys.
 * z-ai-web-dev-sdk is used exclusively in this backend process.
 */
import ZAI from 'z-ai-web-dev-sdk';

const PORT = 8787;
const HOST = '127.0.0.1';

type AnyObj = Record<string, unknown>;
type ZaiClient = Awaited<ReturnType<typeof ZAI.create>>;

let zai: ZaiClient | null = null;
async function client(): Promise<ZaiClient> {
  if (!zai) zai = await ZAI.create();
  return zai;
}

function json(body: AnyObj, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  });
}

function fail(status: number, code: string, message: string): Response {
  console.error(`[ai-bridge] ${status} ${code}: ${message}`);
  return json({ error: { code, message } }, status);
}

/** Python requests OpenAI sizes; map to the nearest supported z-ai size. */
const SIZE_MAP: Record<string, string> = {
  '1024x1536': '864x1152', // portrait
  '1536x1024': '1152x864', // landscape
  '1024x1024': '1024x1024', // square
};
function mapSize(size: unknown): string {
  const raw = typeof size === 'string' && size in SIZE_MAP ? size : '1024x1536';
  return SIZE_MAP[raw];
}

interface BriefResult {
  reply: string;
  brief: string;
}

/** Robustly pull a {reply, brief} JSON object out of a model answer. */
function extractBrief(text: string): BriefResult | null {
  let t = (text || '').trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  const start = t.indexOf('{');
  if (start === -1) return null;
  let depth = 0;
  let inStr = false;
  let esc = false;
  for (let i = start; i < t.length; i++) {
    const c = t[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === '\\') esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) {
        try {
          const obj = JSON.parse(t.slice(start, i + 1));
          if (typeof obj?.reply === 'string' && typeof obj?.brief === 'string') {
            // The studio rejects briefs over 8000 characters; keep it usable.
            return { reply: obj.reply, brief: obj.brief.slice(0, 7900) };
          }
          return null;
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

interface ChatPart {
  type?: string;
  text?: string;
  image_url?: string;
}
interface ChatMessage {
  role: string;
  content: ChatPart[] | string;
}

const JSON_CONTRACT =
  'Respond with ONLY a valid JSON object with exactly two string keys: ' +
  '"reply" (a short helpful explanation of your changes) and "brief" (the complete updated image prompt). ' +
  'No markdown, no code fences, no commentary outside the JSON.';

/** Responses-API payload -> z-ai chat messages. Returns messages + whether images are present. */
function toZaiMessages(payload: AnyObj, systemSuffix = JSON_CONTRACT): { messages: AnyObj[]; hasImage: boolean } {
  const instructions = typeof payload.instructions === 'string' ? payload.instructions : '';
  const input = Array.isArray(payload.input) ? (payload.input as ChatMessage[]) : [];
  const messages: AnyObj[] = [];
  if (instructions) {
    // The z-ai chat API expects system prompts in an 'assistant' role message.
    messages.push({ role: 'assistant', content: `${instructions}${systemSuffix ? '\n\n' + systemSuffix : ''}` });
  }
  let hasImage = false;
  for (const msg of input) {
    const role = msg.role === 'assistant' ? 'assistant' : 'user';
    const parts: ChatPart[] = Array.isArray(msg.content)
      ? msg.content
      : [{ type: 'input_text', text: String(msg.content ?? '') }];
    const texts: string[] = [];
    const images: string[] = [];
    for (const part of parts) {
      if (part.type === 'input_image' && typeof part.image_url === 'string') {
        images.push(part.image_url);
        hasImage = true;
      } else if (typeof part.text === 'string') {
        texts.push(part.text);
      }
    }
    const text = texts.join('\n');
    if (images.length) {
      const content: AnyObj[] = [];
      if (text) content.push({ type: 'text', text });
      for (const url of images) content.push({ type: 'image_url', image_url: { url } });
      messages.push({ role, content });
    } else if (text) {
      messages.push({ role, content: text });
    }
  }
  return { messages, hasImage };
}

function responsesApiWrap(text: string, model: string): AnyObj {
  return {
    id: `resp_${Date.now().toString(36)}`,
    object: 'response',
    created_at: Math.floor(Date.now() / 1000),
    model,
    status: 'completed',
    output: [
      {
        id: `msg_${Date.now().toString(36)}`,
        type: 'message',
        role: 'assistant',
        status: 'completed',
        content: [{ type: 'output_text', text }],
      },
    ],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
  };
}

/** POST /v1/responses — art-direction chat used to refine the artwork brief. */
async function handleResponses(req: Request): Promise<Response> {
  let payload: AnyObj;
  try {
    payload = (await req.json()) as AnyObj;
  } catch {
    return fail(400, 'invalid_json', 'Request body is not valid JSON.');
  }
  const model = typeof payload.model === 'string' ? payload.model : 'glm-4.6';
  const { messages, hasImage } = toZaiMessages(payload);
  if (!messages.length) return fail(400, 'invalid_request', 'No chat input supplied.');
  try {
    const zai = await client();
    const completion = hasImage
      ? await zai.chat.completions.createVision({ messages, thinking: { type: 'disabled' } })
      : await zai.chat.completions.create({ messages, thinking: { type: 'disabled' } });
    const raw = completion.choices[0]?.message?.content ?? '';
    let brief = extractBrief(raw);
    if (!brief) {
      // One normalization pass: ask the model to re-emit its own answer as clean JSON.
      const repair = await zai.chat.completions.create({
        messages: [
          {
            role: 'assistant',
            content:
              'You convert art-direction drafts into strict JSON. ' +
              'Return only a JSON object with string keys "reply" and "brief". ' +
              'Preserve the substance of the draft; never invent a new artwork.',
          },
          { role: 'user', content: raw.slice(0, 12000) },
        ],
        thinking: { type: 'disabled' },
      });
      brief = extractBrief(repair.choices[0]?.message?.content ?? '');
    }
    if (!brief) {
      return fail(502, 'unparsable_brief', 'The model did not return a usable brief.');
    }
    return json(responsesApiWrap(JSON.stringify(brief), model));
  } catch (err) {
    return fail(502, 'provider_error', err instanceof Error ? err.message : String(err));
  }
}

function imageApiResponse(base64: string, model: string): AnyObj {
  return {
    created: Math.floor(Date.now() / 1000),
    model,
    data: [{ b64_json: base64 }],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
  };
}

/** POST /v1/images/generations — generate a master from the approved brief. */
async function handleGenerations(req: Request): Promise<Response> {
  let payload: AnyObj;
  try {
    payload = (await req.json()) as AnyObj;
  } catch {
    return fail(400, 'invalid_json', 'Request body is not valid JSON.');
  }
  const prompt = typeof payload.prompt === 'string' ? payload.prompt.trim() : '';
  if (!prompt) return fail(400, 'invalid_request', 'A prompt is required.');
  const model = typeof payload.model === 'string' ? payload.model : 'cogview-4';
  const size = mapSize(payload.size);
  try {
    const zai = await client();
    const response = await zai.images.generations.create({ prompt, size });
    const base64 = response.data?.[0]?.base64;
    if (!base64) return fail(502, 'no_image', 'No image bytes were returned.');
    return json(imageApiResponse(base64, model));
  } catch (err) {
    return fail(502, 'provider_error', err instanceof Error ? err.message : String(err));
  }
}

/** POST /v1/images/edits — revise an owned reference or the current master (multipart). */
async function handleEdits(req: Request): Promise<Response> {
  let form: FormData;
  try {
    form = await req.formData();
  } catch {
    return fail(400, 'invalid_form', 'Request body must be multipart form data.');
  }
  const file = form.get('image');
  const prompt = String(form.get('prompt') ?? '').trim();
  if (!(file instanceof File)) return fail(400, 'invalid_request', 'An image file is required.');
  if (!prompt) return fail(400, 'invalid_request', 'A prompt is required.');
  const model = String(form.get('model') ?? 'cogview-4');
  const size = mapSize(form.get('size'));
  try {
    const buffer = Buffer.from(await file.arrayBuffer());
    const dataUrl = `data:${file.type || 'image/png'};base64,${buffer.toString('base64')}`;
    const zai = await client();
    const response = await zai.images.generations.edit({
      prompt,
      images: [{ url: dataUrl }],
      size,
    });
    const base64 = response.data?.[0]?.base64;
    if (!base64) return fail(502, 'no_image', 'No image bytes were returned.');
    return json(imageApiResponse(base64, model));
  } catch (err) {
    return fail(502, 'provider_error', err instanceof Error ? err.message : String(err));
  }
}

/** Pull the <svg>…</svg> document out of a model answer (fences tolerated). */
function extractSvg(text: string): string | null {
  let t = (text || '').trim();
  const fence = t.match(/```(?:svg|xml)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  const start = t.indexOf('<svg');
  const end = t.lastIndexOf('</svg>');
  if (start === -1 || end === -1 || end <= start) return null;
  const doc = t.slice(start, end + 6);
  return doc.length > 100 ? doc : null;
}

/** Pull the first balanced JSON object out of a model answer (fences tolerated). */
function extractAnyJson(text: string): AnyObj | null {
  let t = (text || '').trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  const start = t.indexOf('{');
  if (start === -1) return null;
  let depth = 0;
  let inStr = false;
  let esc = false;
  for (let i = start; i < t.length; i++) {
    const c = t[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === '\\') esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) {
        try {
          const obj = JSON.parse(t.slice(start, i + 1));
          return obj && typeof obj === 'object' && !Array.isArray(obj) ? (obj as AnyObj) : null;
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

const GENERIC_JSON_CONTRACT =
  'Respond with ONLY one valid JSON object - no markdown, no code fences, ' +
  'no commentary outside the JSON.';

/** POST /v1/json — generic strict-JSON extraction (scene planner etc.).
 * Tolerates code fences and prose around the object; one repair pass asks
 * the model to re-emit its answer as clean JSON before failing. */
async function handleJson(req: Request): Promise<Response> {
  let payload: AnyObj;
  try {
    payload = (await req.json()) as AnyObj;
  } catch {
    return fail(400, 'invalid_json', 'Request body is not valid JSON.');
  }
  const model = typeof payload.model === 'string' ? payload.model : 'glm-4.6';
  let { messages } = toZaiMessages(payload, GENERIC_JSON_CONTRACT);
  // Honor a responses-style strict json_schema by describing it in the prompt.
  const format = (payload.text as AnyObj | undefined)?.format as AnyObj | undefined;
  const schema = format?.schema;
  if (schema && messages.length) {
    const describe =
      ` The required JSON shape (strict): ${JSON.stringify(schema)}. ` +
      'Return exactly one JSON object matching that shape.';
    messages = [{ ...messages[0], content: String(messages[0].content) + describe }, ...messages.slice(1)];
  }
  if (!messages.length) return fail(400, 'invalid_request', 'No chat input supplied.');
  try {
    const zai = await client();
    const completion = await zai.chat.completions.create({ messages, thinking: { type: 'disabled' } });
    const raw = completion.choices[0]?.message?.content ?? '';
    let obj = extractAnyJson(raw);
    if (!obj) {
      // One normalization pass: ask the model to re-emit its answer as clean JSON.
      const repair = await zai.chat.completions.create({
        messages: [
          {
            role: 'assistant',
            content:
              'You convert drafts into strict JSON. Return only the JSON object, ' +
              'preserving the substance of the draft; never invent new content.',
          },
          { role: 'user', content: raw.slice(0, 12000) },
        ],
        thinking: { type: 'disabled' },
      });
      obj = extractAnyJson(repair.choices[0]?.message?.content ?? '');
    }
    if (!obj) {
      return fail(502, 'unparsable_json', 'The model did not return a usable JSON object.');
    }
    return json(responsesApiWrap(JSON.stringify(obj), model));
  } catch (err) {
    return fail(502, 'provider_error', err instanceof Error ? err.message : String(err));
  }
}

const SVG_CONTRACT =
  'Respond with ONLY the raw SVG document: a single <svg>…</svg> element, no markdown, ' +
  'no fences, no commentary outside the SVG.';

/** POST /v1/svg — separate SVG-generation route (provider authors the master). */
async function handleSvg(req: Request): Promise<Response> {
  let payload: AnyObj;
  try {
    payload = (await req.json()) as AnyObj;
  } catch {
    return fail(400, 'invalid_json', 'Request body is not valid JSON.');
  }
  const model = typeof payload.model === 'string' ? payload.model : 'glm-4.6';
  const { messages, hasImage } = toZaiMessages(payload, SVG_CONTRACT);
  if (!messages.length) return fail(400, 'invalid_request', 'No chat input supplied.');
  try {
    const zai = await client();
    const completion = hasImage
      ? await zai.chat.completions.createVision({ messages, thinking: { type: 'disabled' } })
      : await zai.chat.completions.create({ messages, thinking: { type: 'disabled' } });
    const raw = completion.choices[0]?.message?.content ?? '';
    let svg = extractSvg(raw);
    if (!svg) {
      // One normalization pass: ask the model to re-emit its own art as pure SVG.
      const repair = await zai.chat.completions.create({
        messages: [
          {
            role: 'assistant',
            content:
              'You convert illustration drafts into strict standalone SVG. ' +
              'Return only the single <svg>…</svg> document, preserving the artwork substance. ' +
              'Never invent a new artwork.',
          },
          { role: 'user', content: raw.slice(0, 60000) },
        ],
        thinking: { type: 'disabled' },
      });
      svg = extractSvg(repair.choices[0]?.message?.content ?? '');
    }
    if (!svg) {
      return fail(502, 'unparsable_svg', 'The model did not return a usable SVG document.');
    }
    return json(responsesApiWrap(svg, model));
  } catch (err) {
    return fail(502, 'provider_error', err instanceof Error ? err.message : String(err));
  }
}

const server = Bun.serve({
  port: PORT,
  hostname: HOST,
  idleTimeout: 255, // image generation can legitimately take a while
  async fetch(req): Promise<Response> {
    const url = new URL(req.url);
    try {
      if (req.method === 'GET' && url.pathname === '/healthz') {
        return json({ ok: true, service: 'color-duel-ai-bridge' });
      }
      if (req.method === 'POST' && url.pathname === '/v1/responses') return await handleResponses(req);
      if (req.method === 'POST' && url.pathname === '/v1/json') return await handleJson(req);
      if (req.method === 'POST' && url.pathname === '/v1/svg') return await handleSvg(req);
      if (req.method === 'POST' && url.pathname === '/v1/images/generations') return await handleGenerations(req);
      if (req.method === 'POST' && url.pathname === '/v1/images/edits') return await handleEdits(req);
      return fail(404, 'not_found', `No route: ${req.method} ${url.pathname}`);
    } catch (err) {
      return fail(500, 'bridge_error', err instanceof Error ? err.message : String(err));
    }
  },
});

console.log(`[ai-bridge] listening on http://${HOST}:${PORT} (z-ai backed, OpenAI-compatible shim)`);
