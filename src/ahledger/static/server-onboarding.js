"use strict";
(()=>{
 if(location.pathname==='/login')return;
 const h=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const marketForm=document.getElementById('server-market');
 const select=document.createElement('select');select.id='ahl-market-select';
 const label=document.createElement('label');label.textContent='Выбрать рынок из AHledger';label.append(select);marketForm.prepend(label);
 const note=document.createElement('p');note.className='note';label.after(note);
 const auto=document.createElement('label');auto.className='check';auto.innerHTML='<input name="enable_updates" type="checkbox" checked>Автоматически обновлять цены AHL и пересчитывать возможности';marketForm.append(auto);
 const reload=document.createElement('button');reload.type='button';reload.textContent='Обновить список рынков';note.after(reload);
 const pairing=document.createElement('section');pairing.className='box stack';pairing.id='worker-pairing';pairing.innerHTML='<h2>Подключить игровой ПК</h2><p>В Worker укажите адрес сервера и нажмите «Сохранить и установить аддон» без ключа. Приложение покажет код. Подтвердите здесь тот же код и выберите рынок. Роль выбирается кнопкой в аддоне для каждого персонажа.</p><div id="pairing-pending"></div><div id="discovered-characters"></div>';
 marketForm.closest('section').after(pairing);
 let markets=[];
 async function api(path,method='GET',body){const r=await fetch((window.CennikHubPrefix||'')+'/server/api'+path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return d;}
 function message(value){document.getElementById('server-message').textContent=value;}
 function sync(){const current=marketForm.elements.ahl_market.value;if(![...select.options].some(x=>x.value===current))select.add(new Option(current||'Выберите рынок',current));select.value=current;}
 async function loadMarkets(){reload.disabled=true;try{const result=await api('/ahl/markets');markets=result.items;select.replaceChildren(...markets.map(m=>new Option(m.label+' · '+m.region.toUpperCase()+' · '+m.faction,m.id)));sync();note.textContent='Список AHledger: выбор загружает данные только одного рынка.';}catch(error){sync();note.textContent='Список AHledger недоступен. Сохранённый рынок остаётся выбранным: '+error.message;}finally{reload.disabled=false;}}
 select.addEventListener('change',()=>{const m=markets.find(x=>x.id===select.value);if(!m)return;const f=marketForm.elements;f.ahl_market.value=m.id;for(const key of ['ruleset','faction','region']){if(![...f[key].options].some(x=>x.value===m[key]))f[key].add(new Option(m[key],m[key]));f[key].value=m[key];}if(!f.id.value)f.label.value=m.label;});
 document.addEventListener('click',event=>{if(event.target.dataset.editMarket||event.target.id==='server-new-market')queueMicrotask(sync);});
 reload.addEventListener('click',loadMarkets);
 let busy=false;
 async function pending(){if(busy)return;busy=true;try{const state=await api('/state');const box=document.getElementById('pairing-pending');if(!box.contains(document.activeElement))box.innerHTML=(state.pairing||[]).map(row=>`<form class="pair-approve" data-code="${h(row.code)}"><strong>${h(row.label)} · код ${h(row.code)}</strong><p>Сверьте код с Worker на своём ПК.</p><label>Рынок<select name="profile_id">${state.profiles.map(p=>`<option value="${h(p.id)}">${h(p.label)}</option>`).join('')}</select></label><button>Подтвердить этот ПК</button></form>`).join('')||'<p>Нет ожидающих подключений.</p>';
  document.getElementById('discovered-characters').innerHTML=state.devices.filter(d=>!d.revoked).map(d=>`<div><h3>${h(d.label)}</h3>${(d.discovered||[]).map(c=>`<p>${h(c.character)} · ${h(c.realm)} · ${h({auctioneer:'Аукционер',crafter:'Крафтер',unassigned:'Выберите роль в аддоне'}[c.role])}${c.professions?.length?' · '+h(c.professions.join(', ')):''}</p>`).join('')} ${(d.notes||[]).map(n=>`<p class="notice">${h(n)}</p>`).join('')}</div>`).join('');
 }catch(error){message(error.message);}finally{busy=false;}}
 document.addEventListener('submit',async event=>{const form=event.target;if(!form.classList.contains('pair-approve'))return;event.preventDefault();const button=form.querySelector('button');button.disabled=true;try{await api('/pairing/approve','POST',{code:form.dataset.code,profile_id:form.elements.profile_id.value});message('ПК подтверждён. Выберите роли в аддоне и выполните /reload.');await pending();}catch(error){message(error.message);}finally{button.disabled=false;}});
 loadMarkets();pending();setInterval(()=>{if(!document.hidden)pending();},5000);
})();
