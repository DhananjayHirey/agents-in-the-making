import pg from 'pg';
import bcrypt from 'bcryptjs';
import { config } from '../config.js';
import { SEED_USERS, type Role } from './seed-users.js';

export interface UserRow {
  id: number;
  email: string;
  name: string;
  role: Role;
  is_active: boolean;
  password_hash: string;
}

export let pool: pg.Pool;

/** Creates the database if it does not exist yet (Postgres error 3D000), then connects. */
async function ensureDatabase(url: string): Promise<void> {
  const probe = new pg.Client({ connectionString: url });
  try {
    await probe.connect();
    await probe.end();
  } catch (err: any) {
    if (err?.code !== '3D000') throw err;
    const target = new URL(url);
    const dbName = target.pathname.replace(/^\//, '');
    target.pathname = '/postgres';
    const admin = new pg.Client({ connectionString: target.toString() });
    await admin.connect();
    await admin.query(`CREATE DATABASE "${dbName.replace(/"/g, '')}"`);
    await admin.end();
    console.log(`[postgres] created database ${dbName}`);
  }
}

export async function initPostgres(): Promise<void> {
  await ensureDatabase(config.databaseUrl);
  pool = new pg.Pool({ connectionString: config.databaseUrl });

  await pool.query(`
    CREATE TABLE IF NOT EXISTS users (
      id SERIAL PRIMARY KEY,
      email TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('viewer', 'editor', 'admin')),
      is_active BOOLEAN NOT NULL DEFAULT TRUE,
      password_hash TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )`);

  for (const u of SEED_USERS) {
    await pool.query(
      `INSERT INTO users (email, name, role, password_hash) VALUES ($1, $2, $3, $4)
       ON CONFLICT (email) DO NOTHING`,
      [u.email, u.name, u.role, bcrypt.hashSync(u.password, 10)],
    );
  }
  console.log('[postgres] ready');
}

export async function findUserByEmail(email: string): Promise<UserRow | null> {
  const { rows } = await pool.query<UserRow>('SELECT * FROM users WHERE email = $1', [email]);
  return rows[0] ?? null;
}

export async function findUserById(id: number): Promise<UserRow | null> {
  const { rows } = await pool.query<UserRow>('SELECT * FROM users WHERE id = $1', [id]);
  return rows[0] ?? null;
}
