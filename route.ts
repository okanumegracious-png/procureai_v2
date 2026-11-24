// app/api/boq/upload/route.ts (Next.js Server-Side Proxy to keep API Key secret)
import { NextRequest, NextResponse } from "next/server";

export async function POST(req: NextRequest) {
  try {
    const incomingFormData = await req.formData();
    const backendUrl = process.env.PROCUREAI_BACKEND_URL; // e.g. https://procureai-api.onrender.com
    const apiKey = process.env.PROCUREAI_WEBHOOK_SECRET;

    if (!backendUrl || !apiKey) {
      return NextResponse.json(
        { error: "Server misconfiguration: missing backend credentials" },
        { status: 500 }
      );
    }

    const response = await fetch(`${backendUrl}/api/v1/workflows/upload-boq`, {
      method: "POST",
      headers: {
        "X-API-Key": apiKey,
      },
      body: incomingFormData,
    });

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status });
    }

    return NextResponse.json(data);
  } catch (error: any) {
    return NextResponse.json(
      { error: "Failed to process BOQ upload", details: error.message },
      { status: 500 }
    );
  }
}
