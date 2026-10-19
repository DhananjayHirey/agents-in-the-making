import jwt from 'jsonwebtoken';
import { config } from '../config.js';
import { findUserById } from '../db/postgres.js';
import type { Role } from '../db/seed-users.js';

export interface SessionUser {
  id: number;
  email: string;
  name: string;
  role: Role;
}

export class AuthError extends Error {
  constructor(
    message: string,
    public status: 401 | 403 = 401,
  ) {
    super(message);
  }
}

export function signToken(userId: number): string {
  return jwt.sign({ sub: String(userId) }, config.jwtSecret, { expiresIn: '8h' });
}

/**
 * The single "is this a real, active user?" check — the equivalent of the CMS's
 * SessionService.validateToken. Used by GraphQL (withAuth) AND by the agent relay,
 * so a request reaching the agent has passed exactly the same check as an editor request.
 * Like the CMS, the token is sent raw in the `authorization` header (a "Bearer " prefix is tolerated).
 */
export async function validateToken(rawHeader: string | undefined): Promise<SessionUser> {
  if (!rawHeader) throw new AuthError('Unauthorized: no token');
  const token = rawHeader.replace(/^Bearer\s+/i, '').trim();
  let payload: jwt.JwtPayload;
  try {
    payload = jwt.verify(token, config.jwtSecret) as jwt.JwtPayload;
  } catch {
    throw new AuthError('Unauthorized: invalid or expired token');
  }
  const user = await findUserById(Number(payload.sub));
  if (!user) throw new AuthError('Unauthorized: user not found');
  if (!user.is_active) throw new AuthError('Unauthorized: user is inactive');
  return { id: user.id, email: user.email, name: user.name, role: user.role };
}
