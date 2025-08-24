import { NextResponse } from "next/server";
const BASE = process.env.BACKEND || "http://127.0.0.1:8000";
export async function POST(req: Request) {
  const body = await req.json();
  const r = await fetch(`${BASE}/sp/connect`, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  const text = await r.text();
  return new NextResponse(text, { status: r.status, headers: { "Content-Type": "application/json" }});
}
