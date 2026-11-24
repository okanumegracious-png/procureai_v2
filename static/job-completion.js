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
  .job-report{margin-top:26px;padding:26px;border:1px solid #e4eaf0;border-radius:16px;background:#fff;color:#1e2b3d;box-shadow:0 8px 24px rgba(16,35,55,.045)}
  .job-report h2{margin:6px 0 7px;font:800 22px Manrope,Arial,sans-serif;letter-spacing:-.45px;color:#17263b}.job-report .jr-muted{color:#718096;font-size:12px;line-height:1.65}
  .jr-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:16px 0}
  .jr-grid label,.jr-field{display:flex;flex-direction:column;gap:7px;font-size:12px;font-weight:700;color:#42536a}
  .jr-grid input,.jr-grid textarea,.jr-grid select{width:100%;min-height:42px;border:1px solid #dce4ec;border-radius:9px;padding:11px 12px;font:inherit;font-weight:400;background:#fff;color:#203047;transition:border-color .15s,box-shadow .15s}
  .jr-grid textarea{resize:vertical;line-height:1.6}.jr-grid input:focus,.jr-grid textarea:focus,.jr-grid select:focus{outline:0;border-color:#198566;box-shadow:0 0 0 3px rgba(25,133,102,.12)}
  .jr-wide{grid-column:1/-1}.jr-photo-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:15px 0}
  .jr-photo{display:flex;flex-direction:column;gap:9px;border:1px solid #e1e8ef;border-radius:12px;padding:14px;min-width:0;background:#fbfcfd;color:#34445a;font-size:12px;font-weight:700}
  .jr-photo input[type=file]{width:100%;font-size:11px;font-weight:400;color:#65758a}
  .jr-photo img{display:block;width:100%;height:190px;object-fit:contain;background:#f1f4f7;border-radius:9px;margin:2px 0}
  .jr-photo .jr-placeholder{height:190px;display:grid;place-items:center;background:#f1f4f7;border-radius:9px;color:#8190a2}
  .jr-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:18px;padding-top:16px;border-top:1px solid #edf1f5}
  .jr-btn{min-height:42px;border:1px solid transparent;border-radius:9px;padding:10px 16px;font-weight:700;font-size:12px;cursor:pointer;background:#117b5c;color:#fff;transition:background .15s,transform .15s}
  .jr-btn:hover{background:#0d684d}.jr-btn:focus-visible{outline:3px solid rgba(17,123,92,.25);outline-offset:2px}.jr-btn:disabled{opacity:.55;cursor:wait}
  .jr-btn.secondary{background:#f0f4f7;border-color:#e0e7ee;color:#34465d}.jr-btn.secondary:hover{background:#e7edf3}
  .jr-status{display:inline-flex;align-items:center;padding:6px 10px;border-radius:20px;background:#eaf6f0;color:#176b4e;font-size:11px;font-weight:800}
  .jr-report-view{border-top:1px solid #e7edf2;margin-top:20px;padding-top:18px}
  .jr-saved-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .jr-saved-title{display:grid;gap:5px}.jr-saved-title b{font:700 14px Manrope,Arial,sans-serif;color:#24364c}
  .jr-saved-meta{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 16px}
  .jr-meta-chip{padding:7px 10px;border:1px solid #e5ebf1;border-radius:8px;background:#f8fafc;color:#526176;font-size:11px}
  .jr-saved-sections{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .jr-saved-field{padding:13px;border:1px solid #e8edf2;border-radius:10px;min-width:0}
  .jr-saved-field.wide{grid-column:1/-1}.jr-saved-field small{display:block;margin-bottom:6px;color:#8290a1;font-size:9px;font-weight:800;letter-spacing:.07em;text-transform:uppercase}
  .jr-saved-field p{margin:0;color:#3f5066;font-size:12px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}
  .jr-report-view .jr-photo-grid{margin-top:10px}
  @media(max-width:700px){.jr-grid,.jr-photo-grid,.jr-saved-sections{grid-template-columns:1fr}.jr-wide,.jr-saved-field.wide{grid-column:auto}.job-report{padding:17px}.jr-photo img,.jr-photo .jr-placeholder{height:160px}.jr-actions .jr-btn{flex:1 1 100%}.jr-saved-head{align-items:flex-start}}
  `;
  if(!document.getElementById("jobReportStyles")){const style=document.createElement("style");style.id="jobReportStyles";style.textContent=css;document.head.append(style);}
  let lastProject="",loading=false;
  function notify(msg,bad=false){if(typeof window.showToast==="function")window.showToast(msg,bad);else alert(msg);}
  function fileData(file){return new Promise((resolve,reject)=>{if(!file)return resolve(null);if(file.size>5*1024*1024)return reject(new Error("Each photograph must be 5 MB or smaller."));const reader=new FileReader();reader.onload=()=>resolve({name:file.name,mime_type:file.type||"image/jpeg",data_url:reader.result});reader.onerror=()=>reject(new Error("Could not read the selected photograph."));reader.readAsDataURL(file);});}
  function renderSaved(container,report){
    const photos=[["Before",report.before_photo],["After",report.after_photo]];
    const field=(label,value,wide=false)=>value?'<div class="jr-saved-field'+(wide?' wide':'')+'"><small>'+esc(label)+'</small><p>'+esc(value)+'</p></div>':'';
    container.innerHTML='<div class="jr-report-view">'+
      '<div class="jr-saved-head"><div class="jr-saved-title"><b>Saved job completion report</b><span class="jr-muted">Your report and evidence are saved to this project.</span></div><span class="jr-status">'+esc(report.status||"Draft")+'</span></div>'+
      '<div class="jr-saved-meta"><span class="jr-meta-chip"><b>Job:</b> '+esc(report.job)+'</span><span class="jr-meta-chip"><b>Date:</b> '+esc(report.job_date)+'</span><span class="jr-meta-chip"><b>Location:</b> '+esc(report.location)+'</span></div>'+
      '<div class="jr-saved-sections">'+field("Equipment",report.equipment)+field("Purpose of report",report.purpose,true)+field("Initial condition · before work",report.initial_condition,true)+field("Work carried out",report.work_carried_out,true)+field("Final condition · after work",report.final_condition,true)+field("Technician",report.technician)+field("Supervisor",report.supervisor)+field("Remarks / recommendations",report.remarks,true)+'</div>'+
      '<h3 style="font:700 13px Manrope,Arial,sans-serif;margin:20px 0 8px;color:#24364c">Photographic evidence</h3>'+
      '<div class="jr-photo-grid">'+photos.map(([label,photo])=>'<div class="jr-photo"><b>'+label+' · '+(label==="Before"?"Before work":"After work")+'</b>'+(photo&&photo.data_url?'<img src="'+photo.data_url+'" alt="'+label+' condition photograph"><span class="jr-muted">'+esc(photo.name||"Uploaded photograph")+'</span>':'<div class="jr-placeholder">No photograph uploaded</div>')+'</div>').join("")+'</div>'+
      '<div class="jr-actions"><button type="button" class="jr-btn" data-jr-print>↓ Export / Print PDF</button><span class="jr-muted">Choose “Save as PDF” in the print window to download a copy.</span></div>'+
      '<p class="jr-muted" style="margin-top:12px">Submitted by '+esc(report.recorded_by||"Workspace user")+' · Last updated '+esc(report.updated_at||"")+'</p></div>';
  }
  function printReport(section){
    const report=section.__savedReport;
    if(!report)return notify("Save the report before exporting it.",true);
    // Open the window immediately inside the click handler so browser popup blockers do not suppress it.
    const popup=window.open("","_blank");
    if(!popup)return notify("Your browser blocked the PDF window. Allow pop-ups for ProcureAI, then try again.",true);
    const fields=[
      ["Job",report.job],["Date",report.job_date],["Location",report.location],["Equipment",report.equipment],
      ["Purpose of report",report.purpose],["Initial condition · before work",report.initial_condition],
      ["Work carried out",report.work_carried_out],["Final condition · after work",report.final_condition],
      ["Technician",report.technician],["Supervisor",report.supervisor],["Remarks / recommendations",report.remarks]
    ];
    const body=fields.filter(x=>x[1]).map(([k,v])=>'<section class="report-field"><h3>'+esc(k)+'</h3><p>'+esc(v).replace(/\n/g,"<br>")+'</p></section>').join("");
    const photo=(label,p)=>p&&p.data_url?
      '<figure class="photo"><figcaption><b>'+label+'</b><span>'+esc(p.name||"Uploaded photograph")+'</span></figcaption><img src="'+p.data_url+'" alt="'+label+' condition photograph"></figure>':
      '<figure class="photo"><figcaption><b>'+label+'</b></figcaption><div class="no-photo">No photograph uploaded</div></figure>';
    popup.document.open();
    popup.document.write('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Completion Report · '+esc(report.job||"")+'</title><style>'+
      '@page{size:A4;margin:15mm}*{box-sizing:border-box}body{font:11px Arial,Helvetica,sans-serif;color:#26364a;margin:0;background:#fff}header{padding:0 0 16px;border-bottom:3px solid #147b5d;margin-bottom:20px}.brand{font-size:9px;font-weight:800;letter-spacing:1.8px;color:#147b5d;margin-bottom:8px}h1{font-size:23px;letter-spacing:-.5px;margin:0 0 5px;color:#17283e}header p{font-size:11px;color:#718096;margin:0}.project-meta{margin-top:12px;color:#64748b;font-size:9px;line-height:1.6}.section-title{font-size:14px;color:#1f334a;margin:20px 0 10px;padding-bottom:7px;border-bottom:1px solid #e5ebf1}.details{display:grid;grid-template-columns:1fr 1fr;gap:9px}.report-field{padding:10px 11px;border:1px solid #e4eaf0;border-radius:7px;break-inside:avoid}.report-field h3{font-size:8px;text-transform:uppercase;letter-spacing:.8px;color:#738196;margin:0 0 6px}.report-field p{font-size:10px;line-height:1.6;white-space:pre-wrap;margin:0;overflow-wrap:anywhere}.report-field:nth-child(n+5){grid-column:1/-1}.photos{display:grid;grid-template-columns:1fr 1fr;gap:12px}.photo{margin:0;border:1px solid #dfe6ed;border-radius:8px;padding:9px;break-inside:avoid}.photo figcaption{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:8px;font-size:9px;color:#34465d}.photo figcaption span{color:#8793a3;font-size:8px;text-align:right;overflow-wrap:anywhere}.photo img{display:block;width:100%;height:225px;object-fit:contain;background:#f5f7f9;border-radius:5px}.no-photo{height:225px;display:grid;place-items:center;background:#f5f7f9;color:#8995a4;border-radius:5px}.signatures{display:grid;grid-template-columns:1fr 1fr;gap:25px;margin-top:34px}.signature{border-top:1px solid #aeb9c5;padding-top:7px;color:#718096;font-size:9px}.signature b{display:block;color:#34465d;font-size:10px;margin-bottom:3px}footer{margin-top:26px;padding-top:9px;border-top:1px solid #e5ebf1;color:#8a96a5;font-size:8px;display:flex;justify-content:space-between;gap:10px}@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}'+
      '</style></head><body><header><div class="brand">PROCUREAI · PROJECT DOCUMENTATION</div><h1>Job Completion Report</h1><p>Before &amp; after work documentation</p><div class="project-meta">Project reference: '+esc(section.dataset.projectId||"—")+' &nbsp; · &nbsp; Report status: '+esc(report.status||"Draft")+' &nbsp; · &nbsp; Last updated: '+esc(report.updated_at||"—")+'</div></header>'+
      '<h2 class="section-title">Job information & work record</h2><div class="details">'+body+'</div><h2 class="section-title">Photographic evidence</h2><div class="photos">'+photo("01 · Before work",report.before_photo)+photo("02 · After work",report.after_photo)+'</div>'+
      '<div class="signatures"><div class="signature"><b>'+esc(report.technician||"Technician / personnel")+'</b>Technician / personnel responsible</div><div class="signature"><b>'+esc(report.supervisor||"Supervisor / administrator")+'</b>Supervisor / administrator</div></div>'+
      '<footer><span>Generated by ProcureAI</span><span>Submitted by '+esc(report.recorded_by||"Workspace user")+'</span></footer></body></html>');
    popup.document.close();
    const images=[...popup.document.images];
    const waitForImages=Promise.all(images.map(img=>img.complete?Promise.resolve():new Promise(resolve=>{img.onload=resolve;img.onerror=resolve;})));
    Promise.all([waitForImages,new Promise(resolve=>setTimeout(resolve,500))]).then(()=>{
      popup.focus();
      popup.print();
    });
  }
  async function mount(container,projectId,projectStatus){
    if(loading||!projectId||container.querySelector(".job-report"))return;
    loading=true;lastProject=projectId;
    const section=document.createElement("section");section.className="job-report";section.innerHTML='<div class="jr-muted">JOB CLOSE-OUT · BEFORE & AFTER DOCUMENTATION</div><h2>Job completion report</h2><p class="jr-muted">Record the equipment condition before work, attach the two photographs, and document the work completed and final condition.</p><div class="jr-loading">Loading report…</div>';
    container.append(section);
    try{
      const data=await request(API+"/projects/"+encodeURIComponent(projectId)+"/job-completion-report");
      let saved=data.report;
      if(projectStatus==="terminated"){
        section.innerHTML='<div class="jr-muted">JOB CLOSE-OUT · ARCHIVED RECORD</div><h2>Job completion report</h2><p class="jr-muted">This project is terminated. The report is available for review and PDF export only.</p><div class="jr-existing"></div>';
        if(saved){section.__savedReport=saved;renderSaved(section.querySelector(".jr-existing"),saved);}
        else section.querySelector(".jr-existing").innerHTML='<p class="jr-muted">No job completion report was saved before the project was archived.</p>';
        return;
      }
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
      if(saved){section.__savedReport=saved;renderSaved(section.querySelector(".jr-existing"),saved);}
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
          saved=fresh.report;section.__savedReport=saved;
          renderSaved(section.querySelector(".jr-existing"),fresh.report);
        }catch(e){notify(e.message,true);}finally{submit.disabled=false;}
      }
      section.addEventListener("click",event=>{if(event.target.closest("[data-jr-print]"))printReport(section);});
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
    if(projectId&&(projectId!==lastProject||!content.querySelector(".job-report"))){lastProject=projectId;loading=false;mount(content,projectId,hero.dataset.projectStatus||"active");}
  });
  observer.observe(document.documentElement,{childList:true,subtree:true});
})();