import { useState } from 'react';

const TEST_USERS = [
  { email: 'admin@books.local', label: 'admin@books.local — can delete' },
  { email: 'editor@books.local', label: 'editor@books.local — can edit' },
  { email: 'viewer@books.local', label: 'viewer@books.local — read only' },
];

interface Props {
  onSignIn: (email: string, password: string) => Promise<void>;
}

export function SignIn({ onSignIn }: Props) {
  const [email, setEmail] = useState(TEST_USERS[0].email);
  const [password, setPassword] = useState('');

  return (
    <>
      <strong>Sign in</strong>
      <label htmlFor="user">Test user (passwords in books-backend/src/db/seed-users.ts)</label>
      <select id="user" value={email} onChange={(e) => setEmail(e.target.value)}>
        {TEST_USERS.map((u) => (
          <option key={u.email} value={u.email}>
            {u.label}
          </option>
        ))}
      </select>
      <label htmlFor="pw">Password</label>
      <input id="pw" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <div className="row">
        <button onClick={() => onSignIn(email, password)}>Sign in</button>
      </div>
    </>
  );
}
