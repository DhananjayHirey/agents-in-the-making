import { Router } from 'express';
import bcrypt from 'bcryptjs';
import { findUserByEmail } from '../db/postgres.js';
import { signToken } from '../auth/session.js';

export const authRouter = Router();

/** POST /auth/login { email, password } -> { token, user }. Stand-in for the CMS OTP login. */
authRouter.post('/login', async (req, res) => {
  const { email, password } = req.body ?? {};
  const user = email ? await findUserByEmail(String(email)) : null;
  if (!user || !user.is_active || !bcrypt.compareSync(String(password ?? ''), user.password_hash)) {
    res.status(401).json({ error: 'Invalid email or password' });
    return;
  }
  res.json({ token: signToken(user.id), user: { id: user.id, name: user.name, email: user.email, role: user.role } });
});
