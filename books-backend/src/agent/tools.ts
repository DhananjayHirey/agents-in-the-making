import { createHash } from 'node:crypto';
import { books } from '../db/mongo.js';
import { toGql } from '../graphql/resolvers.js';
import type { SessionUser } from '../auth/session.js';

/**
 * The tools the agent may offer the model. The backend owns this list: change a tool here (or add one when the
 * GraphQL API grows) and the agent picks it up on its next refresh — no agent release needed.
 *
 * Each tool runs ONE fixed GraphQL document with the signed-in user's own token, so withAuth decides access
 * exactly as it does for the UI. The model never gets the schema or a "run any query" tool.
 */

/** What the confirm card shows before a write runs. */
export interface Preview {
  summary: string;
  before: unknown;
  after: unknown;
}

export interface AgentTool {
  name: string;
  description: string;
  /** JSON Schema of the arguments the model passes. */
  inputSchema: Record<string, unknown>;
  /** 'write' tools run only after the user confirms them. */
  kind: 'read' | 'write';
  /** The one GraphQL operation this tool runs. */
  document: string;
  /** Pass all arguments as this one variable (e.g. `input`). Omit to pass each argument as its own variable. */
  argsVariable?: string;
  /** The field of `data` that is the tool's result. */
  resultField: string;
  /** Result objects with these fields are remembered (id -> label) so later turns can say "that book". */
  entity?: { idField: string; labelField: string };
  /** Write tools: builds the confirm card. Runs here, with the data the backend already owns. */
  preview?: (args: Record<string, any>, user: SessionUser) => Promise<Preview>;
}

const BOOK_FIELDS = 'id title author year tags updatedAt updatedBy';
const BOOK_ENTITY = { idField: 'id', labelField: 'title' };

async function currentBook(id: unknown) {
  if (typeof id !== 'string' || !id) return null;
  const doc = await books.findOne({ _id: id });
  return doc ? toGql(doc) : null;
}

export const AGENT_TOOLS: AgentTool[] = [
  {
    name: 'get_book',
    description: "Get one book by its id (a slug such as 'dune').",
    inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
    kind: 'read',
    document: `query GetBook($id: ID!) { getBook(id: $id) { ${BOOK_FIELDS} } }`,
    resultField: 'getBook',
    entity: BOOK_ENTITY,
  },
  {
    name: 'list_books',
    description: "Search books by title, author or tag. Use this to find a book's id.",
    inputSchema: {
      type: 'object',
      properties: {
        search: { type: 'string', description: 'Text to match; omit to list all' },
        limit: { type: 'integer', minimum: 1, maximum: 50 },
      },
    },
    kind: 'read',
    document: `query ListBooks($search: String, $limit: Int) { listBooks(search: $search, limit: $limit) { ${BOOK_FIELDS} } }`,
    resultField: 'listBooks',
    entity: BOOK_ENTITY,
  },
  {
    name: 'set_book',
    description: 'Create or update a book. To update, pass the existing id and the full new values.',
    inputSchema: {
      type: 'object',
      properties: {
        id: { type: 'string', description: 'Existing id to update; omit to create' },
        title: { type: 'string' },
        author: { type: 'string' },
        year: { type: 'integer' },
        tags: { type: 'array', items: { type: 'string' } },
      },
      required: ['title', 'author'],
    },
    kind: 'write',
    document: `mutation SetBook($input: BookInput!) { setBook(input: $input) { ${BOOK_FIELDS} } }`,
    argsVariable: 'input',
    resultField: 'setBook',
    entity: BOOK_ENTITY,
    preview: async (args) => {
      const before = await currentBook(args.id);
      return { summary: `${before ? 'Update' : 'Create'} book '${args.title}'`, before, after: args };
    },
  },
  {
    name: 'delete_book',
    description: 'Delete a book by id.',
    inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
    kind: 'write',
    document: 'mutation DeleteBook($id: ID!) { deleteBook(id: $id) { id deleted } }',
    resultField: 'deleteBook',
    preview: async (args) => {
      const before = await currentBook(args.id);
      return { summary: `Delete book '${before?.title ?? args.id}'`, before, after: null };
    },
  },
];

/** The manifest the agent fetches: every tool without its server-side preview function. */
export const toolManifest = AGENT_TOOLS.map(({ preview: _preview, ...tool }) => tool);

/** Changes whenever any tool changes; the agent sends it back as If-None-Match to skip unchanged refreshes. */
export const toolsVersion = createHash('sha256').update(JSON.stringify(toolManifest)).digest('hex').slice(0, 16);

export function findTool(name: string): AgentTool | undefined {
  return AGENT_TOOLS.find((t) => t.name === name);
}
