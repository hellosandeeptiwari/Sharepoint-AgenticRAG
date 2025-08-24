import { NextResponse } from "next/server";
const BASE = process.env.BACKEND || "http://127.0.0.1:8000";
export async function GET() {
  const r = await fetch(`${BASE}/sp/list`);
  const text = await r.text();
  return new NextResponse(text, { status: r.status, headers: { "Content-Type": "application/json" }});
}
