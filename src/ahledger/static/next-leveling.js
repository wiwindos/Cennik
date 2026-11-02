"use strict";
window.CennikLeveling={
  query:{profile_id:"",profession:"tailoring",skill:null,crafts:1,allow_disenchant:false,disenchanter_id:"",include_unlearned:true,yellow_chance:null,green_chance:null,access:{}},
  result:null,selected:null,limit:100,maxLoss:0,
  exits:{auction:"Продать на аукционе",npc:"Продать NPC",disenchant:"Распылить → аукцион"},
  colors:{orange:"Оранжевый",yellow:"Жёлтый",green:"Зелёный",grey:"Серый",unknown:"Неизвестна"},
  professions:{tailoring:"Портняжное дело",blacksmithing:"Кузнечное дело",leatherworking:"Кожевничество",alchemy:"Алхимия",engineering:"Инженерное дело",enchanting:"Наложение чар",cooking:"Кулинария","first aid":"Первая помощь",mining:"Горное дело",jewelcrafting:"Ювелирное дело"},
  async render(){
    const c=this.context,h=c.h,m=c.money,q=this.query;
    this.data=await c.api('/leveling');
    const profiles=this.data.profiles,profile=profiles.find(p=>p.id===q.profile_id);
    const available=profile?Object.entries(profile.professions).map(([id,p])=>[id,p.name]):Object.entries(this.professions);
    if(available.length&&!available.some(([id])=>id===q.profession))q.profession=available[0][0];
    const skill=profile?.professions[q.profession];
    const options=(empty)=>`<option value="">${empty}</option>`+profiles.map(p=>`<option value="${h(p.id)}" ${q.profile_id===p.id?'selected':''}>${h(p.character)} · ${h(p.realm)}</option>`).join('');
    return `<div class="section-head"><div><h1>Прокачка профессий</h1><p>Сравните рецепты и способы реализации. Покупки выполняются после выбора партии.</p></div></div>
      <section class="box stack"><p>${h(this.data.note)}</p><p class="note">В аддоне: меню под миникартой → «Обновить данные профессий» или /cennik professions. Профессию достаточно открыть — сбор запускается автоматически.</p>
      <form id="leveling-form" class="stack"><div class="fields">
      <label>Персонаж для закупки и крафта<select name="profile_id">${options('Только просмотр — задать навык вручную')}</select></label>
      <label>Профессия<select name="profession">${available.map(([id,name])=>`<option value="${h(id)}" ${q.profession===id?'selected':''}>${h(name)}</option>`).join('')}</select></label>
      <label>Навык вручную, необязательно<input name="skill" type="number" min="0" max="10000" value="${q.skill??''}" placeholder="${skill?skill.skill:'Укажите навык'}"></label>
      <label>Число крафтов<input name="crafts" type="number" min="1" max="1000" required value="${q.crafts}"></label></div>
      ${profile?`<p>Отчёт: ${h(c.date(new Date(profile.observed_at*1000).toISOString()))}. ${skill?h(skill.name)+' · '+skill.skill+'/'+skill.max_skill+' · сохранено рецептов: '+Object.keys(skill.recipes).length:''}${profile.can_purchase?'':' · Для отправки нужен подключённый крафтер Worker.'}</p>`:''}
      <div class="fields"><label class="check"><input name="allow_disenchant" type="checkbox" ${q.allow_disenchant?'checked':''}> Рассматривать распыление</label>
      <label>Кто распыляет<select name="disenchanter_id"><option value="">Тот же персонаж</option>${profiles.filter(p=>p.disenchant).map(p=>`<option value="${h(p.id)}" ${q.disenchanter_id===p.id?'selected':''}>${h(p.character)}</option>`).join('')}</select></label>
      <label class="check"><input name="include_unlearned" type="checkbox" ${q.include_unlearned?'checked':''}> Включить неизученные рецепты</label></div>
      <details><summary>Оценка случайного повышения навыка</summary><p class="note">Игра не сообщает точную вероятность. Можно указать свою оценку; без неё для жёлтых и зелёных рецептов показываем экономику партии без стоимости очка. Это оценка на текущем навыке, сложность может измениться уже после первого крафта.</p><div class="fields">
      <label>Жёлтый: шанс, %<input name="yellow_chance" type="number" min="0.01" max="100" step="0.01" value="${q.yellow_chance===null?'':Number(q.yellow_chance)*100}"></label>
      <label>Зелёный: шанс, %<input name="green_chance" type="number" min="0.01" max="100" step="0.01" value="${q.green_chance===null?'':Number(q.green_chance)*100}"></label></div></details>
      <button class="primary" type="submit">Подобрать варианты</button></form></section>
      ${this.resultsHtml()}`;
  },
  resultsHtml(){
    if(!this.result)return '';
    const c=this.context,h=c.h,m=c.money,r=this.result;
    const rows=r.variants.slice(0,this.limit),selected=r.variants.find(v=>v.id===this.selected);
    return `<section class="box stack"><h2>Варианты · ${r.variants.length}</h2><p>Навык: ${r.skill}. Сначала — минимальные затраты на ожидаемое очко навыка, затем прибыль партии. Ингредиенты покупаются на аукционе.</p>
      ${r.disenchant_warning?`<p class="notice">${h(r.disenchant_warning)}</p>`:''}
      <div class="table-wrap"><table class="leveling-table"><thead><tr><th>Рецепт / сложность</th><th>Реализация</th><th>Затраты</th><th>Чистая выручка</th><th>Прибыль партии</th><th>Затраты / очко</th><th></th></tr></thead><tbody>
      ${rows.map(v=>`<tr><td>${h(v.name)}<br><small>${v.learned?'Изучен':'Нужно изучить'} · ${h(this.colors[v.difficulty])} · ${v.crafts} крафт.</small></td><td>${h(this.exits[v.exit])}</td><td>${m(v.cost_copper)}</td><td>${m(v.net_copper)}</td><td>${m(v.profit_copper)}</td><td>${v.cost_per_skill_copper===null?'Неизвестно':m(Number(v.cost_per_skill_copper))}</td><td><button data-leveling-select="${h(v.id)}">Выбрать</button></td></tr>`).join('')||'<tr><td colspan="7">Подходящих вариантов с ценой реализации нет. Проверьте рецепты, навык и свежесть цен. Серые рецепты не участвуют.</td></tr>'}</tbody></table></div>
      ${r.variants.length>this.limit?'<button data-leveling-more>Показать ещё</button>':''}
      <p class="note">Пропущено: недостаточный навык — ${r.skipped.skill_required||0}; нет повышения — ${r.skipped.no_skill_gain||0}; неполный рецепт — ${r.skipped.incomplete_recipe||0}; нет цены реализации — ${r.skipped.no_exit_price||0}.</p></section>
      ${selected?this.detailHtml(selected):''}`;
  },
  detailHtml(v){
    const c=this.context,h=c.h,m=c.money,a=this.query.access[v.spell_id]||{};
    return `<section class="box stack" id="leveling-detail"><h2>${h(v.name)} → ${h(this.exits[v.exit])}</h2>
      <p>${v.crafts} крафт. → минимум ${v.quantity} шт. Затраты: ${m(v.cost_copper)}; возврат: ${m(v.net_copper)}; прибыль: ${m(v.profit_copper)}.</p>
      <p>${h(v.acquisition)}. ${v.expected_skill_ups===null?'Ожидаемый прирост навыка неизвестен.':'Условный прирост при текущей сложности: '+h(v.expected_skill_ups)+'.'}</p>
      <ul>${v.materials.map(x=>`<li>${h(x.item_name)} × ${x.quantity} · предел ${m(x.unit_price_copper)}/шт.</li>`).join('')}</ul>
      ${!v.learned?`<form id="leveling-access" class="stack" data-spell="${v.spell_id}"><h3>Как получить рецепт</h3><p class="note">Если справочник не связывает рецепт с предметом для изучения, укажите ID этого предмета. Его цена возьмётся с рынка. Если также указано обучение, сравним оба способа. Затраты учитываются один раз на всю партию.</p><div class="fields">
      <label>ID предмета-рецепта<input name="item_id" type="number" min="1" step="1" value="${a.item_id||''}"></label>
      <label>Обучение, золото<input name="trainer_cost_gold" type="number" min="0" step="0.0001" value="${a.trainer_cost_copper===undefined||a.trainer_cost_copper===null?'':a.trainer_cost_copper/10000}"></label></div>
      <label><input name="skill_up_confirmed" type="checkbox" ${a.skill_up_confirmed?'checked':''}> Я проверил: рецепт можно изучить и он повышает навык на этом уровне</label>
      <button type="submit">Учесть получение рецепта</button></form>`:''}
      <ul class="note">${v.warnings.map(w=>`<li>${h(w)}</li>`).join('')}</ul>
      ${v.blockers.length?`<div class="notice"><b>Для отправки требуется:</b><ul>${v.blockers.map(w=>`<li>${h(w)}</li>`).join('')}</ul></div>`:''}
      <label>Допустимый убыток этой партии, золото<input id="leveling-max-loss" type="number" min="0" step="0.0001" value="${this.maxLoss/10000}"></label>
      <p class="note">Общий бюджет: ${m(this.data.budget_copper)}. На аукционе аддон повторно проверит цены, объёмы и предел убытка. Крафт, изучение рецепта и продажу подтверждает игрок.</p>
      <button class="primary" data-leveling-publish="${h(v.id)}" ${v.can_publish&&this.data.profiles.find(p=>p.id===this.query.profile_id)?.can_purchase?'':'disabled'}>Отправить выбранному крафтеру</button></section>`;
  },
  read(form){
    const f=new FormData(form),q=this.query;
    for(const key of ['profile_id','profession','disenchanter_id'])q[key]=String(f.get(key)||'');
    q.skill=f.get('skill')===''?null:Number(f.get('skill'));q.crafts=Number(f.get('crafts'));
    for(const key of ['allow_disenchant','include_unlearned'])q[key]=f.has(key);
    for(const key of ['yellow_chance','green_chance'])q[key]=f.get(key)===''?null:Number(f.get(key))/100;
  },
  async analyze(){this.result=await this.context.post('/leveling/analyze',this.query);this.limit=100;await this.context.open('leveling');},
  bind(c){
    this.context=c;const self=this;
    document.addEventListener('submit',async e=>{
      if(!['leveling-form','leveling-access'].includes(e.target.id))return;
      e.preventDefault();const button=e.target.querySelector('button[type="submit"]');if(button)button.disabled=true;
      try{
        if(e.target.id==='leveling-form')self.read(e.target);
        else{const f=new FormData(e.target);self.query.access[e.target.dataset.spell]={item_id:f.get('item_id')===''?null:Number(f.get('item_id')),trainer_cost_copper:f.get('trainer_cost_gold')===''?null:Math.round(Number(f.get('trainer_cost_gold'))*10000),skill_up_confirmed:f.has('skill_up_confirmed')};}
        await self.analyze();
      }catch(error){c.notify(error.message,true);}finally{if(button)button.disabled=false;}
    });
    document.addEventListener('change',async e=>{
      if(e.target.closest('#leveling-form')&&['profile_id','profession'].includes(e.target.name)){
        self.read(e.target.form);self.query.skill=null;self.query.access={};self.result=null;self.selected=null;await c.open('leveling');
      }
      if(e.target.id==='leveling-max-loss')self.maxLoss=Math.round(Number(e.target.value)*10000);
    });
    document.addEventListener('click',async e=>{
      const b=e.target.closest('[data-leveling-select],[data-leveling-publish],[data-leveling-more]');if(!b)return;
      try{
        if(b.hasAttribute('data-leveling-more'))self.limit+=100;
        if(b.dataset.levelingSelect)self.selected=b.dataset.levelingSelect;
        if(b.dataset.levelingPublish){
          const field=document.getElementById('leveling-max-loss');
          if(!field.checkValidity())throw Error('Укажите допустимый убыток партии');
          self.maxLoss=Math.round(Number(field.value)*10000);b.disabled=true;
          await c.job('/leveling/publish',{analysis_id:self.result.analysis_id,variant_id:b.dataset.levelingPublish,max_loss_copper:self.maxLoss},'Передача партии запущена. Дождитесь Worker и выполните /reload у крафтера.');
        }else await c.open('leveling');
      }catch(error){c.notify(error.message,true);b.disabled=false;}
    });
  }
};
