import SessionWorkspaceClient from "./SessionWorkspaceClient";

/**
 * Server component wrapper for /sessions/[id] workspace.
 *
 * Provides `generateStaticParams` required by Next.js `output: 'export'`.
 */
export function generateStaticParams() {
  return [{ id: "preview" }];
}

interface Props {
  params: Promise<{ id: string }>;
}

export default async function SessionWorkspacePage({ params }: Props) {
  const { id } = await params;
  return <SessionWorkspaceClient sessionId={id} />;
}
