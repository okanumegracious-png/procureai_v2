"use client";

import { FormEvent, useEffect, useState } from "react";
import "./project-workspace.css";

type Milestone = { key: string; label: string; weight: number; completed: boolean; completed_at?: string };
type Update = {
  id: number; author: string; update_type: string; status: string; progress_percent: number;
  field_notes: string; blockers: string; next_steps: string; created_at: string; completed_milestones: string[];
};
type Project = {
  project_id: string; title: string; client_name: string; agency_details: string; executive_summary: string;
  procurement_lead: string; deliverables: string[]; start_date: string; due_date: string; status: string;
  due_status: string; due_label: string; progress_percent: number;
  progress_breakdown: { milestones: number; daily_updates: number };
  milestones: Milestone[]; updates: Update[];
};
type Props = { projectId: string; apiBase?: string; onBack?: () => void };

function csrfToken() {
  if (typeof document === "undefined") return "";
  const item = document.cookie.split("; ").find((part) => part.startsWith("procureai_csrf="));
  return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : "";
}
function dateLabel(value?: string) {
  if (!value) return "Not set";
  const date = new Date(value.length === 10 ? value + "T00:00:00" : value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export default function ProjectWorkspace({ projectId, apiBase = "/api", onBack }: Props) {
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [progress, setProgress] = useState(0);
  const [form, setForm] = useState({ update_type: "daily", status: "In progress", progress_percent: 0, field_notes: "", blockers: "", next_steps: "" });
  const [milestones, setMilestones] = useState<string[]>([]);

  async function refresh() {
    setLoading(true); setError("");
    try {
      const response = await fetch(`${apiBase}/projects/${encodeURIComponent(projectId)}/overview`, { credentials: "include", cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Unable to load project workspace");
      setProject(body.project);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load project workspace");
    } finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, [projectId]);

  async function submitUpdate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setError("");
    try {
      const response = await fetch(`${apiBase}/projects/${encodeURIComponent(projectId)}/updates`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ ...form, completed_milestones: milestones }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not save project update");
      setProject(body.project); setMilestones([]);
      setForm({ update_type: "daily", status: "In progress", progress_percent: 0, field_notes: "", blockers: "", next_steps: "" });
      setProgress(0);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save project update");
    } finally { setSaving(false); }
  }

  if (loading) return <div className="pw-state">Loading project workspace…</div>;
  if (error && !project) return <div className="pw-state pw-error">{error} <button onClick={() => void refresh()}>Retry</button></div>;
  if (!project) return null;

  return (
    <main className="project-workspace-react">
      <div className="pw-toolbar"><button type="button" onClick={onBack}>← All projects</button><span>{project.project_id}</span></div>
      <header className="pw-hero">
        <div><small>PROJECT WORKSPACE</small><h1>{project.title}</h1><p>{project.client_name || "Client not specified"} · Procurement lead: {project.procurement_lead || "Unassigned"}</p></div>
        <span className={`pw-due ${project.due_status}`}>{project.due_label}</span>
      </header>
      <section className="pw-grid">
        <article className="pw-card"><small>PROJECT INTRODUCTION</small><h2>Overview & scope</h2><p>{project.executive_summary || "No executive summary has been added."}</p><dl><dt>Client / agency</dt><dd>{project.client_name || "Not specified"}</dd><dt>Agency details</dt><dd>{project.agency_details || "Not provided"}</dd><dt>Procurement lead</dt><dd>{project.procurement_lead || "Unassigned"}</dd><dt>Start date</dt><dd>{dateLabel(project.start_date)}</dd><dt>Target completion</dt><dd>{dateLabel(project.due_date)}</dd></dl></article>
        <article className="pw-card pw-progress"><small>DELIVERY HEALTH</small><h2>Overall progress</h2><strong>{Math.round(project.progress_percent)}%</strong><div className="pw-track"><i style={{ width: `${project.progress_percent}%` }} /></div><p>Milestones: {project.progress_breakdown.milestones}% · Daily updates: {project.progress_breakdown.daily_updates}%</p></article>
      </section>
      <section className="pw-card"><small>TIMELINE & DUE DATE TRACKER</small><h2>Procurement milestones</h2><div className="pw-milestones">{project.milestones.map((item) => <div key={item.key} className={item.completed ? "done" : ""}><b>{item.completed ? "✓" : "○"} {item.label}</b><span>{item.completed ? "Completed" : "Awaiting completion"}</span></div>)}</div></section>
      <section className="pw-card"><small>EXPECTED OUTPUTS</small><h2>Key deliverables</h2>{project.deliverables.length ? <ul>{project.deliverables.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>No deliverables listed.</p>}</section>
      <section className="pw-activity">
        <article className="pw-card"><small>TEAM JOURNAL</small><h2>Daily activity log</h2>{project.updates.length ? project.updates.map((item) => <div className="pw-update" key={item.id}><div className="pw-update-head"><b>{item.author}</b><time>{new Date(item.created_at).toLocaleString()}</time></div><span>{item.update_type.replace("_", " ")} · {item.status} · {item.progress_percent}%</span>{item.field_notes && <p>{item.field_notes}</p>}{item.blockers && <p className="pw-blocker"><b>Blocker:</b> {item.blockers}</p>}{item.next_steps && <p><b>Next:</b> {item.next_steps}</p>}</div>) : <p>No updates have been submitted yet.</p>}</article>
        <article className="pw-card"><small>ADD AN ENTRY</small><h2>Submit daily update</h2>{error && <p className="pw-error">{error}</p>}
          <form onSubmit={submitUpdate}>
            <label>Update type<select value={form.update_type} onChange={(e) => setForm({ ...form, update_type: e.target.value })}><option value="daily">Daily update</option><option value="field_note">Field note</option><option value="blocker">Blocker / issue</option><option value="milestone">Milestone update</option></select></label>
            <label>Status<input value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })} required /></label>
            <label>Reported workstream progress · {progress}%<input type="range" min="0" max="100" step="5" value={progress} onChange={(e) => { const value = Number(e.target.value); setProgress(value); setForm({ ...form, progress_percent: value }); }} /></label>
            <label>Field notes<textarea rows={4} value={form.field_notes} onChange={(e) => setForm({ ...form, field_notes: e.target.value })} /></label>
            <label>Blockers / risks<textarea rows={2} value={form.blockers} onChange={(e) => setForm({ ...form, blockers: e.target.value })} /></label>
            <label>Next steps<textarea rows={2} value={form.next_steps} onChange={(e) => setForm({ ...form, next_steps: e.target.value })} /></label>
            <fieldset><legend>Milestones completed</legend>{project.milestones.map((item) => <label className="pw-check" key={item.key}><input type="checkbox" disabled={item.completed} checked={milestones.includes(item.key)} onChange={(e) => setMilestones(e.target.checked ? [...milestones, item.key] : milestones.filter((key) => key !== item.key))} />{item.label}</label>)}</fieldset>
            <button type="submit" disabled={saving}>{saving ? "Saving…" : "Post project update"}</button>
          </form>
        </article>
      </section>
    </main>
  );
}
