import { MongoClient, type Collection } from 'mongodb';
import { config } from '../config.js';

export interface BookDoc {
  _id: string; // human-readable slug id, e.g. "dune"
  title: string;
  author: string;
  year?: number;
  tags: string[];
  updatedAt: Date;
  updatedBy: string;
}

export let books: Collection<BookDoc>;

const SEED_BOOKS: Omit<BookDoc, 'updatedAt' | 'updatedBy'>[] = [
  { _id: 'dune', title: 'Dune', author: 'Frank Herbert', year: 1965, tags: ['sci-fi', 'classic'] },
  { _id: 'the-hobbit', title: 'The Hobbit', author: 'J. R. R. Tolkien', year: 1937, tags: ['fantasy'] },
  { _id: 'clean-code', title: 'Clean Code', author: 'Robert C. Martin', year: 2008, tags: ['programming'] },
  { _id: 'project-hail-mary', title: 'Project Hail Mary', author: 'Andy Weir', year: 2021, tags: ['sci-fi'] },
];

export async function initMongo(): Promise<void> {
  const client = new MongoClient(config.mongoUrl);
  await client.connect();
  books = client.db(config.mongoDb).collection<BookDoc>('books');
  if ((await books.countDocuments()) === 0) {
    await books.insertMany(SEED_BOOKS.map((b) => ({ ...b, updatedAt: new Date(), updatedBy: 'seed' })));
    console.log('[mongo] seeded books');
  }
  console.log('[mongo] ready');
}
