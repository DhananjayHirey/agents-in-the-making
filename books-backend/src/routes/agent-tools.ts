import { Router, type NextFunction, type Request, type Response } from 'express';
import { timingSafeEqual } from 'node:crypto';
import { config } from '../config.js';
import { AuthError, validateToken } from '../auth/session.js';
import { findTool, toolManifest, toolsVersion } from '../agent/tools.js';

/**
 * Internal API for the agent service (never the browser): the agent's tool list, and confirm-card previews.
 *
 *   GET  /internal/agent-tools                 the manifest; ETag = version, If-None-Match -> 304
 *   POST /internal/agent-tools/:name/preview   { args } -> { summary, before, after }, as the user (their token)
 */
export const agentToolsRouter = Router();

function sameSecret(given: string | undefined): boolean {
  const a = Buffer.from(given ?? '');
  const b = Buffer.from(config.internalAuthSecret);
  return a.length === b.length && timingSafeEqual(a, b);
}

// Only the agent knows the shared secret.
agentToolsRouter.use((req: Request, res: Response, next: NextFunction) => {
  if (!sameSecret(req.header('x-internal-auth'))) {
    res.status(401).json({ error: 'Missing or wrong internal auth' });
    return;
  }
  next();
});

agentToolsRouter.get('/', (req, res) => {
  const etag = `"${toolsVersion}"`;
  res.setHeader('etag', etag);
  if (req.header('if-none-match') === etag) {
    res.status(304).end();
    return;
  }
  res.json({ version: toolsVersion, tools: toolManifest });
});

agentToolsRouter.post('/:name/preview', async (req, res) => {
  let user;
  try {
    user = await validateToken(req.header('authorization'));
  } catch (err) {
    res.status(err instanceof AuthError ? err.status : 401).json({ error: (err as Error).message });
    return;
  }
  const tool = findTool(req.params.name);
  if (!tool?.preview) {
    res.status(404).json({ error: `No write tool named '${req.params.name}'` });
    return;
  }
  try {
    res.json(await tool.preview(req.body?.args ?? {}, user));
  } catch (err) {
    console.error('[agent-tools] preview failed', err);
    res.status(500).json({ error: 'Preview failed' });
  }
});
