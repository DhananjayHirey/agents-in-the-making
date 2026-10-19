export const typeDefs = /* GraphQL */ `
  type User {
    id: Int!
    email: String!
    name: String!
    role: String!
  }

  type Book {
    id: ID!
    title: String!
    author: String!
    year: Int
    tags: [String!]!
    updatedAt: String!
    updatedBy: String!
  }

  input BookInput {
    "Omit to create a new book; the id is derived from the title."
    id: ID
    title: String!
    author: String!
    year: Int
    tags: [String!]
  }

  type DeleteResult {
    id: ID!
    deleted: Boolean!
  }

  type Query {
    "Who am I? The agent calls this to validate the user (like the CMS validateSession)."
    me: User!
    getBook(id: ID!): Book
    listBooks(search: String, limit: Int = 20): [Book!]!
  }

  type Mutation {
    "Create or update a book. Editor or admin."
    setBook(input: BookInput!): Book!
    "Delete a book. Admin only."
    deleteBook(id: ID!): DeleteResult!
  }
`;
