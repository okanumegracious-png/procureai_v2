/* Project-level before-and-after job completion reports. */
(() => {
  const API="/api";
  const csrf=()=>{const p=document.cookie.split("; ").find(x=>x.startsWith("procureai_csrf="));return p?decodeURIComponent(p.slice("procureai_csrf=".length)):"";};
  async function request(url,options={}) {
    const headers=new Headers(options.headers||{});
    if(options.method&&options.method!=="GET"&&csrf()) headers.set("X-CSRF-Token",csrf());
    const res=await fetch(url,{...options,headers,credentials:"same-origin",cache:"no-store"});
    const raw=await res.text(); let data={}; try{data=raw?JSON.parse(raw):{};}catch{throw new Error("The report service returned an unexpected response (HTTP "+res.status+").");}
    if(!res.ok) throw new Error(data.detail||"Report request failed (HTTP "+res.status+").");
    return data;
  }
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const css=`
  .job-report{margin-top:24px;padding:24px;border:1px solid #dfe7e2;border-radius:16px;background:#fff;color:#20312a}
  .job-report h2{margin:5px 0 8px;font-size:22px}.job-report .jr-muted{color:#718078;font-size:13px;line-height:1.55}
  .jr-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:16px 0}
  .jr-grid label,.jr-field{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:600}
  .jr-grid input,.jr-grid textarea,.jr-grid select{width:100%;border:1px solid #d7e0da;border-radius:9px;padding:10px;font:inherit;background:#fff;color:#20312a}
  .jr-wide{grid-column:1/-1}.jr-photo-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:14px 0}
  .jr-photo{border:1px dashed #b9c8bf;border-radius:12px;padding:12px;min-width:0}.jr-photo img{display:block;width:100%;height:190px;object-fit:contain;background:#f4f7f5;border-radius:8px;margin:10px 0}
  .jr-photo .jr-placeholder{height:190px;display:grid;place-items:center;background:#f4f7f5;border-radius:8px;color:#819087}
  .jr-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.jr-btn{border:0;border-radius:9px;padding:11px 16px;font-weight:700;cursor:pointer;background:#203c32;color:#fff}.jr-btn.secondary{background:#edf2ef;color:#203c32}.jr-status{display:inline-block;padding:5px 9px;border-radius:20px;background:#edf2ef;font-size:12px;font-weight:700}
  .jr-report-view{border-top:1px solid #e6ece8;margin-top:18px;padding-top:16px}.jr-report-view p{white-space:pre-wrap;line-height:1.55}
  @media(max-width:700px){.jr-grid,.jr-photo-grid{grid-template-columns:1fr}.jr-wide{grid-column:auto}.job-report{padding:16px}.jr-photo img,.jr-photo .jr-placeholder{height:150px}}
  `;
  if(!document.getElementById("jobReportStyles")){const style=document.createElement("style");style.id="jobReportStyles";style.textContent=css;document.head.append(style);}
  let lastProject="",loading=false;
  function notify(msg,bad=false){if(typeof window.showToast==="function")window.showToast(msg,bad);else alert(msg);}
  function fileData(file){return new Promise((resolve,reject)=>{if(!file)return resolve(null);if(file.size>5*1024*1024)return reject(new Error("Each photograph must be 5 MB or smaller."));const reader=new FileReader();reader.onload=()=>resolve({name:file.name,mime_type:file.type||"image/jpeg",data_url:reader.result});reader.onerror=()=>reject(new Error("Could not read the selected photograph."));reader.readAsDataURL(file);});}
  function renderSaved(container,report){
    const photos=[["Before",report.before_photo],["After",report.after_photo]];
    container.innerHTML=`<div class="jr-report-view"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><b>Saved job completion report</b><span class="jr-status">${esc(report.status)}</span></div>
      <p><b>Job:</b> ${esc(report.job)} &nbsp; <b>Date:</b> ${esc(report.job_date)}<br><b>Location:</b> ${esc(report.location)}<br><b>Equipment:</b> ${esc(report.equipment)}</p>
      <p><b>Purpose of report</b><br>${esc(report.purpose)}</p><p><b>Initial condition</b><br>${esc(report.initial_condition)}</p>
      <p><b>Work carried out</b><br>${esc(report.work_carried_out)}</p><p><b>Final condition</b><br>${esc(report.final_condition||"Not recorded")}</p>
      <div class="jr-photo-grid">${photos.map(([label,photo])=>`<div class="jr-photo"><b>${label} photograph</b>${photo&&photo.data_url?`<img src="${photo.data_url}" alt="${label} condition photograph"><small>${esc(photo.name||label)}</small>`:'<div class="jr-placeholder">No photograph uploaded</div>'}</div>`).join("")}</div>
      <p class="jr-muted">Submitted by ${esc(report.recorded_by||"Workspace user")} · ${esc(report.updated_at||"")}</p></div>`;
  }
  async function mount(container,projectId,projectStatus){
    if(loading||!projectId||container.querySelector(".job-report"))return;
    loading=true;lastProject=projectId;
    const section=document.createElement("section");section.className="job-report";section.innerHTML='<div class="jr-muted">JOB CLOSE-OUT · BEFORE & AFTER DOCUMENTATION</div><h2>Job completion report</h2><p class="jr-muted">Record the equipment condition before work, attach the two photographs, and document the work completed and final condition.</p><div class="jr-loading">Loading report…</div>';
    container.append(section);
    try{
      const data=await request(API+"/projects/"+encodeURIComponent(projectId)+"/job-completion-report");
      const saved=data.report;
      section.innerHTML=`<div class="jr-muted">JOB CLOSE-OUT · BEFORE & AFTER DOCUMENTATION</div><h2>Job completion report</h2><p class="jr-muted">This report is attached to this project and retained in its history.</p>
        <div class="jr-existing"></div>
        <form class="jr-form">
          <div class="jr-grid">
            <label>Job<input name="job" required maxlength="220" placeholder="e.g. Repair of industrial water pump" value="${esc(saved?.job||"")}"></label>
            <label>Date<input name="job_date" type="date" required value="${esc(saved?.job_date||new Date().toISOString().slice(0,10))}"></label>
            <label>Location<input name="location" required maxlength="240" placeholder="Job site / facility" value="${esc(saved?.location||"")}"></label>
            <label>Equipment<input name="equipment" required maxlength="240" placeholder="Equipment name, model or asset ID" value="${esc(saved?.equipment||"")}"></label>
            <label class="jr-wide">Purpose of report<textarea name="purpose" rows="3" required placeholder="Why was this job undertaken and what outcome was expected?">${esc(saved?.purpose||"")}</textarea></label>
            <label class="jr-wide">Initial condition (before work)<textarea name="initial_condition" rows="4" required placeholder="Faults, defects, damage, operational status and observations before work began.">${esc(saved?.initial_condition||"")}</textarea></label>
          </div>
          <b>Photography record</b><p class="jr-muted">Upload exactly two photographs: one before the product/equipment was fixed and one after it was fixed. JPG, PNG or WebP; maximum 5 MB each.</p>
          <div class="jr-photo-grid">
            <label class="jr-photo">Photo 1 — Before<input type="file" name="before_photo" accept="image/jpeg,image/png,image/webp" ${saved?.before_photo?"":"required"}><div class="jr-preview-before"></div></label>
            <label class="jr-photo">Photo 2 — After<input type="file" name="after_photo" accept="image/jpeg,image/png,image/webp" ${saved?.after_photo?"":"required"}><div class="jr-preview-after"></div></label>
          </div>
          <div class="jr-grid">
            <label class="jr-wide">Work carried out<textarea name="work_carried_out" rows="5" required placeholder="Repairs, parts replaced, servicing, installation, adjustments and tests performed.">${esc(saved?.work_carried_out||"")}</textarea></label>
            <label class="jr-wide">Final condition after work<textarea name="final_condition" rows="4" placeholder="Operational status, tests/results, outstanding issues and readiness for use.">${esc(saved?.final_condition||"")}</textarea></label>
            <label>Technician / personnel responsible<input name="technician" maxlength="180" value="${esc(saved?.technician||"")}"></label>
            <label>Supervisor / administrator<input name="supervisor" maxlength="180" value="${esc(saved?.supervisor||"")}"></label>
            <label class="jr-wide">Remarks / recommendations<textarea name="remarks" rows="2">${esc(saved?.remarks||"")}</textarea></label>
          </div>
          <div class="jr-actions"><button type="button" class="jr-btn secondary" data-jr-draft>Save draft</button><button type="submit" class="jr-btn">Submit completion report</button></div>
          <p class="jr-muted">A submitted report is retained with its photographs. Project closure remains a separate administrator action.</p>
        </form>`;
      if(saved)renderSaved(section.querySelector(".jr-existing"),saved);
      const form=section.querySelector("form");
      for(const [field,preview] of [["before_photo",".jr-preview-before"],["after_photo",".jr-preview-after"]]){
        const input=form.elements[field],box=form.querySelector(preview);
        if(saved?.[field])box.innerHTML=`<img style="width:100%;max-height:180px;object-fit:contain" src="${saved[field].data_url}" alt="Previously saved ${field.replace("_"," ")}">`;
        input.addEventListener("change",()=>{const f=input.files[0];if(!f){box.innerHTML="";return;}if(!f.type.startsWith("image/")){input.value="";notify("Please select an image file.",true);return;}const url=URL.createObjectURL(f);box.innerHTML=`<img style="width:100%;max-height:180px;object-fit:contain" src="${url}" alt="Selected photograph preview">`;});
      }
      async function save(status){
        const submit=form.querySelector('[type="submit"]');submit.disabled=true;
        try{
          const fd=new FormData(form),payload={};for(const [k,v] of fd.entries())if(!(v instanceof File))payload[k]=v;
          payload.status=status;
          const before=form.elements.before_photo.files[0],after=form.elements.after_photo.files[0];
          payload.before_photo=before?await fileData(before):null;payload.after_photo=after?await fileData(after):null;
          if(saved){if(!payload.before_photo)payload.before_photo=saved.before_photo;if(!payload.after_photo)payload.after_photo=saved.after_photo;}
          if(status==="Submitted"&&(!payload.before_photo||!payload.after_photo))throw new Error("Both before and after photographs are required before submission.");
          const result=await request(API+"/projects/"+encodeURIComponent(projectId)+"/job-completion-report",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
          notify(status==="Draft"?"Job report draft saved":"Job completion report submitted");
          const fresh=await request(API+"/projects/"+encodeURIComponent(projectId)+"/job-completion-report");
          renderSaved(section.querySelector(".jr-existing"),fresh.report);
        }catch(e){notify(e.message,true);}finally{submit.disabled=false;}
      }
      form.querySelector("[data-jr-draft]").addEventListener("click",()=>save("Draft"));
      form.addEventListener("submit",e=>{e.preventDefault();save("Submitted");});
    }catch(e){section.innerHTML='<div class="jr-muted">JOB CLOSE-OUT</div><h2>Job completion report</h2><p>'+esc(e.message)+'</p>';}
    finally{loading=false;}
  }
  const observer=new MutationObserver(()=>{
    const content=document.getElementById("projectWorkspaceContent");
    if(!content)return;
    const hero=content.querySelector(".project-workspace-hero");
    const projectId=hero?.querySelector(".eyebrow")?.textContent.match(/PROJECT WORKSPACE\s*·\s*(\S+)/)?.[1];
    if(projectId&&projectId!==lastProject){lastProject=projectId;loading=false;mount(content,projectId,hero.textContent);}
  });
  observer.observe(document.documentElement,{childList:true,subtree:true});
})();