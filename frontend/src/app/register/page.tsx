"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useSeo } from "@/components/Seo";

export default function RegisterPage() {
  useSeo({
    title: "Create Account — Earl Knows Ball",
    description: "Create your Earl Knows Ball account to unlock AI handicapping chat, premium game picks, and expert analysis.",
    keywords: "create account, sign up, Earl Knows Ball, premium picks, AI betting",
  });
  const router = useRouter();

  useEffect(() => {
    router.replace("/");
  }, [router]);

  return null;
}
