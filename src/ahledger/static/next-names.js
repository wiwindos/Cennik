"use strict";
(() => {
  const prefix=location.pathname.match(/^(?:\/cennik)?\/m\/[a-f0-9]{32}/)?.[0]||"";
  if(!document.getElementById('content'))return;
  const fetchRaw=window.fetch.bind(window);
  const professions={tailoring:['Портняжное дело','Tailoring'],blacksmithing:['Кузнечное дело','Blacksmithing'],leatherworking:['Кожевничество','Leatherworking'],alchemy:['Алхимия','Alchemy'],engineering:['Инженерное дело','Engineering'],enchanting:['Наложение чар','Enchanting'],cooking:['Кулинария','Cooking'],'first aid':['Первая помощь','First Aid'],mining:['Горное дело','Mining'],jewelcrafting:['Ювелирное дело','Jewelcrafting'],herbalism:['Травничество','Herbalism'],skinning:['Снятие шкур','Skinning'],fishing:['Рыбная ловля','Fishing']};
  let language='ruRU',key='cennik.game-language.local',catalog={item:{},recipe:{},npc:{}},revision=null,lastCheck=0,loading=null;
  function catalogStatus(error){const status=document.getElementById('game-language-status');if(status){status.hidden=!error;status.textContent=error?'Названия временно недоступны':'';}}
  function profession(value){const normalized=String(value||'').toLowerCase();const row=professions[normalized]||Object.values(professions).find(p=>p.some(n=>n.toLowerCase()===normalized));return row?row[language==='ruRU'?0:1]:value;}
  function label(kind,id,fallback){const entry=catalog[kind]?.[String(id)];if(entry?.[language])return entry[language];const other=language==='ruRU'?'enUS':'ruRU';if(entry?.[other])return entry[other]+' ['+(other==='ruRU'?'RU':'EN')+']';return fallback&&!/\[(?:RU|EN|без перевода)\]$/.test(fallback)?fallback+' [без перевода]':fallback;}
  function transform(value,hint=''){
    if(Array.isArray(value))return value.map(v=>transform(v,hint));
    if(!value||typeof value!=='object')return value;
    if(hint==='names'&&Object.entries(value).every(([id,name])=>/^\d+$/.test(id)&&typeof name==='string'))return Object.fromEntries(Object.entries(value).map(([id,name])=>[id,label('item',id,name)]));
    const result={};for(const [k,v] of Object.entries(value))result[k]=transform(v,k);
    const item=value.item_id??value.result_item_id??value.material_item_id??(hint==='items'?value.id:null);
    const spell=value.spell_id??value.recipe_id;
    if('item_name' in value)result.item_name=label('item',item,value.item_name);
    if('recipe_name' in value)result.recipe_name=label('recipe',spell,value.recipe_name);
    if('name' in value){
      if(spell)result.name=label('recipe',spell,label('item',item,value.name));
      else if(item)result.name=label('item',item,value.name);
      else if(value.npc_id)result.name=label('npc',value.npc_id,value.name);
      else if(professions[hint])result.name=profession(hint);
    }
    if('npc_name' in value)result.npc_name=label('npc',value.npc_id,value.npc_name);
    return result;
  }
  async function refresh(force=false){
    if(loading)return loading;
    if(!force&&Date.now()-lastCheck<15000)return;
    loading=(async()=>{try{const r=await fetchRaw(prefix+'/api/v2/game-names'+(revision?'?revision='+encodeURIComponent(revision):''));if(!r.ok)throw Error('Не удалось загрузить игровые названия');const data=await r.json();if(data.names)catalog=data.names;revision=data.revision;catalogStatus(false);}catch{catalogStatus(true);}finally{lastCheck=Date.now();}})().finally(()=>loading=null);
    return loading;
  }
  const ready=(async()=>{
    if(window.CennikHubPrefix){const r=await fetchRaw('/auth/me');if(r.ok){const me=await r.json();key='cennik.game-language.'+me.username;}}
    try{const saved=localStorage.getItem(key);if(['ruRU','enUS'].includes(saved))language=saved;}catch{}
    const select=document.createElement('select');select.id='game-language';select.setAttribute('aria-label','Язык игровых названий');select.innerHTML='<option value="ruRU">Названия: RU</option><option value="enUS">Названия: EN</option>';select.value=language;
    select.onchange=()=>{language=select.value;try{localStorage.setItem(key,language);}catch{}location.reload();};
    document.querySelector('.header-actions').append(select);
    const status=document.createElement('small');status.id='game-language-status';status.hidden=true;status.setAttribute('role','status');document.querySelector('.header-actions').append(status);
    await refresh(true);
  })();
  window.CennikNames={ready,label,profession,transform,refresh,get language(){return language;}};
  window.fetch=async(input,options)=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href);
    const localized=url.origin===location.origin&&url.pathname.startsWith(prefix+'/api/v2/')&&!/\/(settings|sources|npc\/catalog|disenchant\/rules|game-names)(\/|$)/.test(url.pathname);
    if(localized)await ready;
    const response=await fetchRaw(input,options);
    if(localized&&response.ok&&response.headers.get('content-type')?.includes('application/json')){
      await refresh();const json=response.json.bind(response);response.json=async()=>transform(await json());
    }
    return response;
  };
})();
