import { redirect } from "next/navigation";

export default async function TeamDetailRedirect({ params }: { params: Promise<{ abbreviation: string }> }) {
  const { abbreviation } = await params;
  redirect(`/nfl/teams/${abbreviation}`);
}
