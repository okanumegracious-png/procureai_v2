// components/BoqReviewInterface.tsx
"use client";

import React, { useState } from "react";

interface BOQItem {
  item_code: string;
  item_name: string;
  specification: string;
  quantity: number;
  unit: string;
  category: string;
}

export default function BoqReviewInterface({ projectId }: { projectId: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [items, setItems] = useState<BOQItem[]>([]);
  const [summary, setSummary] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleUploadAndExtract = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;

    setLoading(true);
    setError(null);

    try {
      const formData = new FormData();
      formData.append("project_id", projectId);
      formData.append("file", file);

      // Calls Next.js server route (which attaches X-API-Key securely)
      const res = await fetch("/api/boq/upload", {
        method: "POST",
        body: formData,
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || "Extraction failed");

      setSummary(data.project_summary);
      setItems(data.items);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleQuantityChange = (index: number, newQty: number) => {
    const updated = [...items];
    updated[index].quantity = newQty;
    setItems(updated);
  };

  return (
    <div className="max-w-5xl mx-auto p-6 bg-white rounded-xl shadow-sm border border-gray-200">
      <h2 className="text-xl font-semibold text-gray-900 mb-4">BOQ Upload & Review</h2>

      <form onSubmit={handleUploadAndExtract} className="flex items-center gap-4 mb-6">
        <input
          type="file"
          accept=".pdf,.csv,.xlsx"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
          className="block w-full text-sm text-gray-600 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
        />
        <button
          type="submit"
          disabled={!file || loading}
          className="px-5 py-2.5 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? "Extracting..." : "Extract BOQ"}
        </button>
      </form>

      {error && (
        <div className="p-3 mb-4 text-sm text-red-700 bg-red-50 rounded-lg border border-red-200">
          {error}
        </div>
      )}

      {summary && (
        <div className="mb-4 p-3 bg-gray-50 rounded-lg text-sm text-gray-700">
          <strong>Scope Summary:</strong> {summary}
        </div>
      )}

      {items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50 text-xs uppercase text-gray-600">
                <th className="py-3 px-4">Code</th>
                <th className="py-3 px-4">Material / Description</th>
                <th className="py-3 px-4">Specification</th>
                <th className="py-3 px-4">Category</th>
                <th className="py-3 px-4">Quantity</th>
                <th className="py-3 px-4">Unit</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 text-sm">
              {items.map((item, idx) => (
                <tr key={idx} className="hover:bg-gray-50">
                  <td className="py-3 px-4 font-mono text-xs">{item.item_code}</td>
                  <td className="py-3 px-4 font-medium text-gray-900">{item.item_name}</td>
                  <td className="py-3 px-4 text-gray-600">{item.specification}</td>
                  <td className="py-3 px-4">
                    <span className="px-2 py-1 text-xs rounded-full bg-blue-50 text-blue-700">
                      {item.category}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <input
                      type="number"
                      value={item.quantity}
                      onChange={(e) => handleQuantityChange(idx, parseFloat(e.target.value) || 0)}
                      className="w-24 px-2 py-1 border border-gray-300 rounded text-right"
                    />
                  </td>
                  <td className="py-3 px-4 text-gray-600">{item.unit}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
