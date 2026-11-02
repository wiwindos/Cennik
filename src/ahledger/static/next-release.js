"use strict";
(()=>{
 const h=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 async function api(path,method='GET',body){const r=await fetch((window.CennikPrefix||'')+'/api/v2'+path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return d;}
 function message(value,error=false){const box=document.getElementById('message');box.hidden=false;box.className=error?'error':'';box.textContent=value;}
 window.CennikRelease={
  panel(page,state){
   const w=state.overview.settings,active=w.own_data_only;
   if(page==='opportunities')return `<section class="box"><label class="check"><input id="own-data-mode" type="checkbox" ${active?'checked':''}>Работать без AHledger</label><p class="note">Собственные цены и сохранённые справочники. Ликвидность без оценок продаж отмечается как неизвестная. ${active?'После изменения режима пересчитайте возможности.':''}</p></section>`;
   if(!['settings','sources'].includes(page))return '';
   const status=state.overview.source_updates||{},set=state.overview.upstreams?.ahl_token_set;
   return `<section class="box stack" id="upstream-settings"><h2>Обновления источников</h2>
    <p>Цены AHledger обновляются при включённом автообновлении цен. Рецепты — раз в час; история рабочих предметов — небольшими партиями. Каталог FYI проверяется раз в час независимо от наличия цен AHL.</p>
    ${[['auto_references','Обновлять рецепты AHledger'],['auto_history','Накапливать историю рабочих предметов'],['auto_fyi','Обновлять предметы и NPC-цены FYI']].map(([key,label])=>`<label class="check"><input type="checkbox" data-upstream-setting="${key}" ${w[key]?'checked':''}>${label}</label>`).join('')}
    <button type="button" data-fyi-update>Проверить каталог FYI сейчас</button>
    <p class="note">Старая бета-сборка не становится релизной из-за даты скачивания. При ошибке остаётся последняя проверенная версия.</p>
    <div>${Object.entries(status).map(([kind,row])=>`<p>${h({fyi:'Каталог FYI',references:'Рецепты AHL',history:'История AHL'}[kind]||kind)}: ${h({ok:'Обновлено',error:'Ошибка — прежние данные сохранены',running:'Выполняется'}[row.state]||row.state)}${row.state==='error'&&row.message?' · '+h(row.message):''}${row.attempted_at?' · '+h(new Date(row.attempted_at*1000).toLocaleString('ru-RU')):''}</p>`).join('')||'<p>Автоматические проверки ещё не выполнялись.</p>'}</div>
    <form id="ahl-token-form" class="stack"><h3>Токен AHledger</h3><p id="ahl-token-status">${set?'Токен сохранён':'Токен не задан — публичные запросы'}</p><label>Новый токен<input name="token" type="password" autocomplete="new-password" maxlength="500" placeholder="ahlk_…"></label><div class="actions"><button>Сохранить токен</button><button type="button" data-ahl-check>Проверить подключение</button><button type="button" data-ahl-clear>Удалить токен</button></div><p class="note">Хранится только на сервере. Пустое поле оставляет прежний токен. Успешный запрос не подтверждает увеличение квоты.</p></form></section>`;
  }
 };
 document.addEventListener('change',async event=>{
  const field=event.target,key=field.id==='own-data-mode'?'own_data_only':field.dataset.upstreamSetting;if(!key)return;
  field.disabled=true;
  try{const current=await api('/settings');current.workspace[key]=field.checked;await api('/settings','PUT',current.workspace);message(key==='own_data_only'?'Режим сохранён. Пересчитайте возможности.':'Автообновление сохранено.');await window.CennikRelease.changed();}
  catch(error){field.checked=!field.checked;message(error.message,true);}finally{field.disabled=false;}
 });
 document.addEventListener('submit',async event=>{if(event.target.id!=='ahl-token-form')return;event.preventDefault();const input=event.target.elements.token,button=event.target.querySelector('button');button.disabled=true;try{const d=await api('/sources/ahl/token','PUT',{token:input.value});input.value='';document.getElementById('ahl-token-status').textContent=d.ahl_token_set?'Токен сохранён':'Токен не задан';message('Настройки токена сохранены.');}catch(error){message(error.message,true);}finally{button.disabled=false;}});
 document.addEventListener('click',async event=>{const b=event.target.closest('[data-fyi-update],[data-ahl-clear],[data-ahl-check]');if(!b)return;b.disabled=true;try{if(b.hasAttribute('data-fyi-update')){await api('/sources/fyi/update','POST',{});message('Проверка FYI поставлена в очередь.');}else if(b.hasAttribute('data-ahl-clear')){await api('/sources/ahl/token','PUT',{clear:true});document.getElementById('ahl-token-status').textContent='Токен удалён';message('Токен удалён.');}else{await api('/sources/ahl/check','POST',{});message('Проверка соединения поставлена в очередь.');}}catch(error){message(error.message,true);}finally{b.disabled=false;}});
})();
