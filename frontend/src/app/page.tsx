import { LoginButton } from "../../lib/auth";

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-zinc-50 dark:bg-black">
      <main className="flex flex-col items-center gap-8 px-6 text-center">
        <h1 className="text-4xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
          Haunter
        </h1>
        <p className="max-w-sm text-zinc-600 dark:text-zinc-400">
          Autonomous CI failure diagnosis and fix agent. Sign in with GitHub to
          connect your repos.
        </p>
        {/* LoginButton is a Client Component — reads /auth/me via httpOnly cookie */}
        <LoginButton />
      </main>
    </div>
  );
}
