import { GraphQLError } from 'graphql';
import { books, type BookDoc } from '../db/mongo.js';
import { AuthError, validateToken, type SessionUser } from '../auth/session.js';
import type { Role } from '../db/seed-users.js';

export interface GqlContext {
  authorization?: string;
}

type Resolver<A = any> = (parent: unknown, args: A, ctx: GqlContext & { user: SessionUser }) => unknown;

/**
 * Same shape as the CMS's withAuth(resolver, allowedRoles): every protected resolver
 * re-validates the token itself. Requests from the agent carry the user's own token,
 * so they get exactly the access that user has in the UI — no more.
 */
function withAuth<A>(resolver: Resolver<A>, allowed: Role[]) {
  return async (parent: unknown, args: A, ctx: GqlContext) => {
    let user: SessionUser;
    try {
      user = await validateToken(ctx.authorization);
    } catch (err) {
      const message = err instanceof AuthError ? err.message : 'Unauthorized';
      throw new GraphQLError(message, { extensions: { code: 'UNAUTHENTICATED' } });
    }
    if (!allowed.includes(user.role)) {
      throw new GraphQLError(`Access denied: role '${user.role}' cannot do this`, {
        extensions: { code: 'FORBIDDEN' },
      });
    }
    return resolver(parent, args, { ...ctx, user });
  };
}

const ANY: Role[] = ['viewer', 'editor', 'admin'];

function toGql(doc: BookDoc) {
  return { ...doc, id: doc._id, updatedAt: doc.updatedAt.toISOString() };
}

function slugify(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
}

export const resolvers = {
  Query: {
    me: withAuth((_p, _a, ctx) => ctx.user, ANY),

    getBook: withAuth<{ id: string }>(async (_p, { id }) => {
      const doc = await books.findOne({ _id: id });
      return doc ? toGql(doc) : null;
    }, ANY),

    listBooks: withAuth<{ search?: string; limit?: number }>(async (_p, { search, limit }) => {
      const filter = search
        ? {
            $or: [
              { title: { $regex: search, $options: 'i' } },
              { author: { $regex: search, $options: 'i' } },
              { tags: search.toLowerCase() },
            ],
          }
        : {};
      const docs = await books
        .find(filter)
        .sort({ title: 1 })
        .limit(Math.min(limit ?? 20, 50))
        .toArray();
      return docs.map(toGql);
    }, ANY),
  },

  Mutation: {
    setBook: withAuth<{ input: { id?: string; title: string; author: string; year?: number; tags?: string[] } }>(
      async (_p, { input }, ctx) => {
        const id = input.id ?? slugify(input.title);
        const update = {
          title: input.title,
          author: input.author,
          year: input.year ?? undefined,
          tags: input.tags ?? [],
          updatedAt: new Date(),
          updatedBy: ctx.user.email,
        };
        await books.updateOne({ _id: id }, { $set: update }, { upsert: true });
        const saved = await books.findOne({ _id: id });
        return toGql(saved!);
      },
      ['editor', 'admin'],
    ),

    deleteBook: withAuth<{ id: string }>(
      async (_p, { id }) => {
        const res = await books.deleteOne({ _id: id });
        return { id, deleted: res.deletedCount === 1 };
      },
      ['admin'],
    ),
  },
};
