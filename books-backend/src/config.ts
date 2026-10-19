import 'dotenv/config';

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing env var ${name}. Copy .env.example to .env and fill it in.`);
  return value;
}

export const config = {
  port: Number(process.env.PORT ?? 4000),
  databaseUrl: required('DATABASE_URL'),
  mongoUrl: process.env.MONGO_URL ?? 'mongodb://localhost:27017',
  mongoDb: process.env.MONGO_DB ?? 'books_poc',
  jwtSecret: required('JWT_SECRET'),
  agentUrl: process.env.AGENT_URL ?? 'http://localhost:8001',
  internalAuthSecret: required('INTERNAL_AUTH_SECRET'),
};
