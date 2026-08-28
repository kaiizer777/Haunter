export function generateStaticParams() {
  return [{ id: "placeholder" }];
}

import RunDetailClient from "./RunDetailClient";

export default function Page() {
  return <RunDetailClient />;
}
