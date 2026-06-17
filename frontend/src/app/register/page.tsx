"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, display_name: email.split("@")[0] }),
      });
      const data = await res.json();
      localStorage.setItem("earl_token", data.access_token);
      router.push("/chat");
    } catch {
      alert("Registration failed");
    }
  }

  return (
    <div className="max-w-md mx-auto pt-24 space-y-6">
      <div className="text-center space-y-2">
        <h1 className="font-display text-3xl font-bold">Register</h1>
        <p className="text-gray-400 text-sm">Create your account</p>
      </div>
      <form onSubmit={handleRegister} className="border border-white/10 rounded-xl p-6 bg-white/5 space-y-3">
        <input
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          className="w-full px-4 py-2 rounded-lg bg-black/50 border border-white/10 text-sm focus:outline-none focus:border-earl-500"
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          className="w-full px-4 py-2 rounded-lg bg-black/50 border border-white/10 text-sm focus:outline-none focus:border-earl-500"
        />
        <button type="submit" className="w-full py-2 rounded-lg bg-earl-600 text-white font-semibold hover:bg-earl-500 transition">
          Register
        </button>
        <p className="text-center text-sm text-gray-500 mt-4">
          Already have an account?{" "}
          <a href="/login" className="text-earl-400 hover:underline">Login</a>
        </p>
      </form>
    </div>
  );
}
