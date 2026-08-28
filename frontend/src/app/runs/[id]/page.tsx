export function generateStaticParams() {
  return [{ id: "placeholder" }];
}

import RunDetailClient from "./RunDetailClient";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return <RunDetailClient params={params} />;
}
