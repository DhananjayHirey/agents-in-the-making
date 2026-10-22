import express from 'express';
import cors from 'cors';
import { ApolloServer } from '@apollo/server';
import { expressMiddleware } from '@apollo/server/express4';
import { config } from './config.js';
import { initPostgres } from './db/postgres.js';
import { initMongo } from './db/mongo.js';
import { typeDefs } from './graphql/schema.js';
import { resolvers, type GqlContext } from './graphql/resolvers.js';
import { authRouter } from './routes/auth.js';
import { agentRelay } from './routes/agent-relay.js';
import { agentToolsRouter } from './routes/agent-tools.js';

async function main() {
  await initPostgres();
  await initMongo();

  const app = express();
  app.use(cors());
  app.use(express.json());

  const apollo = new ApolloServer<GqlContext>({ typeDefs, resolvers });
  await apollo.start();
  app.use(
    '/graphql',
    expressMiddleware(apollo, {
      // The raw `authorization` header is all a resolver needs; withAuth validates it.
      context: async ({ req }) => ({ authorization: req.header('authorization') }),
    }),
  );

  app.use('/auth', authRouter);
  app.use('/api/agent', agentRelay);
  app.use('/internal/agent-tools', agentToolsRouter); // agent service only (shared secret)

  app.listen(config.port, () => {
    console.log(`books-backend on http://localhost:${config.port}  (GraphQL at /graphql; chat UI is books-frontend)`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
