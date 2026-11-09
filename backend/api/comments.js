// Shared comment store for the review site. Comments arrive already encrypted in the
// browser (AES-GCM, key derived from the site password), so this function only ever
// sees opaque ciphertext. Access requires a bearer token that is also derived from the
// site key; only its SHA-256 is configured here (COMMENTS_TOKEN_SHA256).
//
// All comments live in ONE private blob. Reads go through the Blob CDN cache (cache
// hits are free on the Hobby plan); each change is a single upload. Hobby includes
// 2,000 uploads/lists and 10,000 cache-miss reads per month, and Blob is blocked for
// 30 days if those run out, so this file never calls list() and never reads per comment.
import { createHash, timingSafeEqual } from 'node:crypto';
import { put, get, BlobPreconditionFailedError } from '@vercel/blob';

const DOC_PATH = 'comments/all.json';
const CACHE_SECONDS = 60;            // the shortest Blob CDN cache: others see changes within ~1-2 min
const ID_RE = /^[\w-]{1,64}$/;
const MAX_DATA = 20000, MAX_BATCH = 1000, MAX_COMMENTS = 5000;
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || 'https://repcreative.github.io').split(',').map((s) => s.trim());

function cors(origin) {
  const h = {
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type, If-None-Match',
    'Access-Control-Expose-Headers': 'ETag',
    'Access-Control-Max-Age': '600',
    'Cache-Control': 'no-store',
    Vary: 'Origin',
  };
  if (origin && (ALLOWED_ORIGINS.includes(origin) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin))) {
    h['Access-Control-Allow-Origin'] = origin;
  }
  return h;
}
function reply(req, status, body, extra) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...cors(req.headers.get('origin')), ...(extra || {}) },
  });
}
function authorized(req) {
  const expected = process.env.COMMENTS_TOKEN_SHA256 || '';
  const m = /^Bearer ([0-9a-f]{64})$/.exec(req.headers.get('authorization') || '');
  if (!m || !/^[0-9a-f]{64}$/.test(expected)) return false;
  return timingSafeEqual(createHash('sha256').update(m[1]).digest(), Buffer.from(expected, 'hex'));
}
function validRecord(r) {
  return r && typeof r.id === 'string' && ID_RE.test(r.id) && typeof r.data === 'string' && r.data.length <= MAX_DATA;
}

// The latest version this instance wrote. A warm instance serves it when the CDN copy
// is older, so a change shows up for everyone at once instead of after the cache window.
let memo = null;

async function readDoc({ fresh, ifNoneMatch } = {}) {
  const r = await get(DOC_PATH, { access: 'private', useCache: !fresh, ifNoneMatch: ifNoneMatch || undefined });
  if (!r) return { comments: [], etag: null, updatedAt: '' };
  // get() can report a weak ETag (W/"…"), but conditional writes only accept the strong form
  const etag = String(r.blob.etag || '').replace(/^W\//, '') || null;
  if (r.statusCode === 304) return { notModified: true, etag };
  const doc = JSON.parse(await new Response(r.stream).text());
  return { comments: Array.isArray(doc.comments) ? doc.comments.filter(validRecord) : [], etag, updatedAt: String(doc.updatedAt || '') };
}

export function OPTIONS(req) {
  return new Response(null, { status: 204, headers: cors(req.headers.get('origin')) });
}

export async function GET(req) {
  if (!authorized(req)) return reply(req, 401, { error: 'unauthorized' });
  const inm = (req.headers.get('if-none-match') || '').replace(/^W\//, '');
  let doc = await readDoc({ ifNoneMatch: memo ? undefined : inm });
  if (memo && !(doc.updatedAt >= memo.updatedAt)) doc = memo;
  if (inm && doc.etag === inm) doc = { notModified: true, etag: doc.etag };
  const etagHeader = doc.etag ? { ETag: doc.etag } : {};
  if (doc.notModified) return reply(req, 304, null, etagHeader);
  return reply(req, 200, { updatedAt: doc.updatedAt, comments: doc.comments }, etagHeader);
}

// Body: { put: [{id, data}], del: [id] }. Applies the batch with optimistic locking and
// returns the full, fresh list so the writer sees its change at once.
export async function POST(req) {
  if (!authorized(req)) return reply(req, 401, { error: 'unauthorized' });
  let body;
  try { body = await req.json(); } catch { return reply(req, 400, { error: 'bad json' }); }
  const puts = Array.isArray(body && body.put) ? body.put : [];
  const dels = Array.isArray(body && body.del) ? body.del : [];
  if (puts.length + dels.length > MAX_BATCH || !puts.every(validRecord) || !dels.every((d) => typeof d === 'string' && ID_RE.test(d))) {
    return reply(req, 400, { error: 'bad batch' });
  }
  const now = new Date().toISOString();
  for (let attempt = 0; attempt < 6; attempt++) {
    const doc = await readDoc({ fresh: true });
    const map = new Map(doc.comments.map((r) => [r.id, r]));
    dels.forEach((id) => map.delete(id));
    puts.forEach((r) => map.set(r.id, { id: r.id, data: r.data, savedAt: now }));
    if (map.size > MAX_COMMENTS) return reply(req, 413, { error: 'too many comments' });
    const comments = [...map.values()];
    try {
      const res = await put(DOC_PATH, JSON.stringify({ version: 1, updatedAt: now, comments }), {
        access: 'private', addRandomSuffix: false, contentType: 'application/json',
        cacheControlMaxAge: CACHE_SECONDS,
        ...(doc.etag ? { allowOverwrite: true, ifMatch: doc.etag } : { allowOverwrite: false }),
      });
      memo = { comments, etag: res.etag, updatedAt: now };
      return reply(req, 200, { updatedAt: now, comments }, res.etag ? { ETag: res.etag } : {});
    } catch (e) {
      const conflict = e instanceof BlobPreconditionFailedError || /already exists|precondition/i.test(String(e && e.message));
      if (!conflict) throw e;
      await new Promise((r) => setTimeout(r, 100 + Math.random() * 300));
    }
  }
  return reply(req, 409, { error: 'busy, try again' });
}
