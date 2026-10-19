// Test users for the POC. Local development only — never reuse these anywhere else.
// Roles mirror the CMS idea: viewer reads, editor also writes, admin also deletes.
export type Role = 'viewer' | 'editor' | 'admin';

export const SEED_USERS: Array<{ email: string; name: string; role: Role; password: string }> = [
  { email: 'admin@books.local', name: 'Asha Admin', role: 'admin', password: 'admin-pass-123' },
  { email: 'editor@books.local', name: 'Eli Editor', role: 'editor', password: 'editor-pass-123' },
  { email: 'viewer@books.local', name: 'Vik Viewer', role: 'viewer', password: 'viewer-pass-123' },
];
