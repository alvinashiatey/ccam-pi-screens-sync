const $ = s => document.querySelector(s);
const state = { token: sessionStorage.getItem('piSyncToken') || '', config: null, devices: {}, videos: [] };
const api = async (path, options = {}) => {
  options.headers = {...options.headers, 'X-Pi-Sync-Token': state.token};
  if (options.json) { options.body = JSON.stringify(options.json); options.headers['Content-Type'] = 'application/json'; }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { $('#tokenDialog').showModal(); throw new Error('Token required'); }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
};
const bytes = n => n > 1e9 ? `${(n/1e9).toFixed(1)} GB` : `${(n/1e6).toFixed(0)} MB`;
const toast = (message, error=false) => { const el=$('#toast'); el.textContent=message; el.className=`toast show${error?' error':''}`; clearTimeout(el.timer); el.timer=setTimeout(()=>el.className='toast',4200); };
const selected = () => [...document.querySelectorAll('.device input:checked')].map(i=>i.value);

async function load() {
  try {
    const [config, status, media] = await Promise.all([api('/api/config'), api('/api/status'), api('/api/videos')]);
    state.config=config; state.devices=status.devices; state.videos=media.videos;
    renderDevices(); renderVideos();
  } catch (e) { if (e.message !== 'Token required') toast(e.message,true); }
}
function renderDevices() {
  const entries=Object.entries(state.devices); const online=entries.filter(([,d])=>d.online).length;
  $('#onlineCount').textContent=`${online} / ${entries.length}`;
  const master=state.devices[state.config.master];
  $('#masterLabel').textContent=`${state.config.master} · ${master?.online ? 'online' : 'offline'}`;
  $('#deviceGrid').innerHTML=entries.map(([name,d],i)=>`<label class="device" style="animation-delay:${i*25}ms">
    <input type="checkbox" value="${name}"><div class="number">${name}</div>${name===state.config.master?'<span class="master-tag">MASTER</span>':''}
    <div class="address">${d.address}</div><footer><div class="badge ${d.online?'online':'offline'}"><i></i>${d.online?(d.mode==='sync'?'synced':'local'):'offline'}</div>
    <small>${d.online ? `${d.sync?.drift_ms!=null?`${d.sync.drift_ms>0?'+':''}${d.sync.drift_ms} ms`:d.clock_offset||'clock —'} · ${d.disk_free?bytes(d.disk_free)+' free':''}` : (d.error||'unreachable')}</small></footer></label>`).join('');
}
function renderVideos() {
  const select=$('#videoSelect'), prior=select.value;
  select.innerHTML='<option value="">Choose a video</option>'+state.videos.map(v=>`<option value="${escapeHtml(v.name)}">${escapeHtml(v.name)}</option>`).join('');
  if (state.videos.some(v=>v.name===prior)) select.value=prior;
  $('#library').innerHTML=state.videos.map(v=>`<div class="media-row"><strong>${escapeHtml(v.name)}</strong><span>${bytes(v.size)}</span><button data-video="${escapeHtml(v.name)}">SELECT</button></div>`).join('');
  document.querySelectorAll('[data-video]').forEach(b=>b.onclick=()=>{$('#videoSelect').value=b.dataset.video; window.scrollTo({top:300,behavior:'smooth'});});
}
const escapeHtml = s => String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
async function action(action) {
  let devices=selected(); const video=$('#videoSelect').value;
  if (!devices.length) return toast('Select at least one display.',true);
  if(action==='sync'&&!devices.includes(state.config.master)) devices=[...devices,state.config.master];
  if (action!=='stop'&&!video) return toast('Choose a video first.',true);
  try { const r=await api('/api/action',{method:'POST',json:{action,devices,video}}); const failures=Object.entries(r.results).filter(([,v])=>v.ok===false); toast(failures.length?`${failures.length} display(s) could not be updated.`:`Command sent to ${devices.length} display(s).`,!!failures.length); setTimeout(load,800); } catch(e){toast(e.message,true)}
}
async function upload(file) {
  if (!file) return; const transfer=$('#transfer'), bar=$('#transferBar'), text=$('#transferText'); transfer.hidden=false;
  try {
    text.textContent=`Uploading ${file.name} to kiosk9…`; bar.style.width='12%';
    const uploaded=await api('/api/upload',{method:'POST',body:file,headers:{'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'}}); bar.style.width='55%';
    const clients=Object.keys(state.config.devices).filter(n=>n!==state.config.master); text.textContent=`Verifying copies across ${clients.length} screens…`;
    const distributed=await api('/api/distribute',{method:'POST',json:{video:uploaded.name,devices:clients}}); bar.style.width='100%';
    const failed=Object.values(distributed.results).filter(v=>!v.ok).length; text.textContent=failed?`Stored on kiosk9 · ${failed} transfer(s) failed`:'Verified on every display'; toast(text.textContent,!!failed); await load();
  } catch(e){bar.style.width='100%';text.textContent=e.message;toast(e.message,true)}
}

$('#refreshButton').onclick=load; $('#syncButton').onclick=()=>action('sync'); $('#localButton').onclick=()=>action('local'); $('#stopButton').onclick=()=>action('stop');
$('#selectAll').onchange=e=>document.querySelectorAll('.device input').forEach(i=>i.checked=e.target.checked);
$('#chooseFile').onclick=()=>$('#fileInput').click(); $('#fileInput').onchange=e=>upload(e.target.files[0]);
const drop=$('#dropZone'); ['dragenter','dragover'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.add('drag')})); ['dragleave','drop'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.remove('drag')})); drop.addEventListener('drop',e=>upload(e.dataTransfer.files[0]));
$('#tokenForm').onsubmit=e=>{e.preventDefault();state.token=$('#tokenInput').value;sessionStorage.setItem('piSyncToken',state.token);$('#tokenDialog').close();load()};
if(!state.token) $('#tokenDialog').showModal(); else load(); setInterval(()=>state.token&&load(),10000);
