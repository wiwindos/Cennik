"use strict";
(()=>{
  const h=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let state=null;
  const message=v=>document.getElementById('server-message').textContent=v;
  async function api(path,method='GET',body){const r=await fetch((window.CennikHubPrefix||'')+path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const d=await r.json();if(!r.ok){if(r.status===401&&location.pathname!=='/login')location.href='/login';throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));}return d;}
  async function load(){
    state=await api('/server/api/state');
    document.getElementById('server-profiles').innerHTML=state.profiles.map(p=>`<div class="box"><h3>${h(p.label)}</h3><p>${h(p.realm_names.join(', ')||'Имя игрового сервера ещё не привязано')} · ${h(p.faction)} · ${h(p.region.toUpperCase())}${p.id===state.ahl_profile_id?' · выбран для AHledger':''}</p><div class="actions"><a href="${window.CennikHubPrefix||''}/m/${p.id}/">Открыть полный Cennik</a><button data-edit-market="${p.id}">Настроить</button></div></div>`).join('');
    for(const select of document.querySelectorAll('.profile-select'))select.innerHTML=state.profiles.map(p=>`<option value="${p.id}">${h(p.label)}</option>`).join('');
    document.getElementById('server-workers').innerHTML=state.devices.map(d=>`<div class="box"><strong>${h(d.label)}</strong><p>${d.revoked?'Ключ отозван':d.last_seen?'Последняя связь: '+h(new Date(d.last_seen*1000).toLocaleString()):'Ещё не подключался'}</p>${d.revoked?'':`<button data-revoke-worker="${d.id}">Отозвать ключ</button>`}</div>`).join('');
  }
  document.addEventListener('submit',async e=>{
    const form=e.target,formId=form.getAttribute('id')||'';if(!formId.startsWith('server-'))return;e.preventDefault();const f=new FormData(form),button=form.querySelector('button[type="submit"],button:not([type])');if(button)button.disabled=true;
    try{
      if(formId==='server-login'){await api('/auth/login','POST',{key:f.get('key')});location.href='/';return;}
      if(formId==='server-market'){
        const profile={id:f.get('id')||crypto.randomUUID().replaceAll('-',''),label:f.get('label'),ruleset:f.get('ruleset'),faction:f.get('faction'),region:f.get('region'),client_family:f.get('client_family')||'forever',realm_names:String(f.get('realm_names')).split('\n').map(v=>v.trim()).filter(Boolean),ahl_market:String(f.get('ahl_market')).trim()||null};
        await api('/server/api/markets','POST',{profile,ahl_source:f.has('ahl_source'),enable_updates:f.has('enable_updates')});message('Рынок сохранён. Данные других серверов остаются в отдельных профилях');await load();
      }else if(formId==='server-worker'){
        const clients=String(f.get('clients')).split('\n').filter(v=>v.trim()).map(line=>{const [role,character,realm,account='']=line.split('|').map(v=>v.trim());return {role,character,realm,account};});
        const d=await api('/server/api/markets/'+f.get('profile_id')+'/workers','POST',{label:f.get('label'),clients});
        const box=document.getElementById('worker-invitation');box.hidden=false;box.textContent='Cennik Worker\nСервер: '+state.worker_url+'\nКлюч (показывается один раз): '+d.token;await load();
      }else if(formId==='server-collector'){
        const d=await api('/server/api/collectors','POST',{label:f.get('label'),profile_id:f.get('profile_id')});const box=document.getElementById('collector-invitation');box.hidden=false;box.textContent='Cennik Collector\nСервер: '+state.collector_url+'\nЛичный ключ (показывается один раз): '+d.token;
      }
    }catch(error){message(error.message);}finally{if(button)button.disabled=false;}
  });
  document.addEventListener('click',async e=>{try{
    if(e.target.id==='server-logout'){if(window.CennikHubPrefix){const me=await (await fetch('/auth/me')).json();await fetch('/auth/logout',{method:'POST',headers:{'X-CSRF-Token':me.csrf}});}else await api('/auth/logout','POST',{});location.href='/login';}
    if(e.target.id==='server-new-market'){document.getElementById('server-market').reset();document.querySelector('#server-market [name="id"]').value='';}
    if(e.target.dataset.editMarket){const p=state.profiles.find(p=>p.id===e.target.dataset.editMarket),form=document.getElementById('server-market');for(const key of ['id','label','ruleset','faction','region','ahl_market','client_family'])form.elements[key].value=p[key]||'';form.elements.realm_names.value=p.realm_names.join('\n');form.elements.ahl_source.checked=p.id===state.ahl_profile_id;form.scrollIntoView({behavior:'smooth'});}
    if(e.target.dataset.revokeWorker){await api('/server/api/workers/'+e.target.dataset.revokeWorker,'DELETE');await load();}
  }catch(error){message(error.message);}});
  if(location.pathname!=='/login')load().catch(e=>message(e.message));
})();
