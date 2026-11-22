"use strict";
window.CennikVendor={
  defaults:{q:'',sort:'closeness',order:'asc',min_markup:'',max_markup:'10',max_age_seconds:0,stackable_only:false},
  filters:{q:'',sort:'closeness',order:'asc',min_markup:'',max_markup:'10',max_age_seconds:0,stackable_only:false},offset:0,
  query(){const values={...this.filters,offset:this.offset,limit:50};for(const key of ['min_markup','max_markup'])if(values[key]==='')delete values[key];return new URLSearchParams(values);},
  unit(value){if(value===null||value===undefined)return '—';const negative=value<0,n=Math.abs(Number(value)),g=Math.floor(n/10000),s=Math.floor((n-g*10000)/100),c=n-g*10000-s*100;return (negative?'−':'')+(g?g.toLocaleString('ru-RU')+' з ':'')+(s?s+' с ':'')+(c>0.00001||(!g&&!s)?c.toLocaleString('ru-RU',{maximumFractionDigits:4})+' м':'');},
  percent(value){return (value>0?'+':'')+Number(value).toLocaleString('ru-RU',{maximumFractionDigits:4})+'%';},
  save(){try{localStorage.setItem(this.preferenceKey,JSON.stringify(this.filters));}catch{}},
  async render(){
    const c=this.context,h=c.h,f=this.filters,data=await c.api('/vendor-bargains?'+this.query()),s=data.summary;this.data=data;
    const titles={closeness:'Близость к NPC',markup:'Наценка к NPC, %',price:'Минимальная цена аука',vendor:'Цена продажи NPC',quantity:'Предложение, шт.',name:'Название'};
    const header=(key,title)=>`<button class="link" data-vendor-sort="${key}">${title}${f.sort===key?(f.order==='asc'?' ↑':' ↓'):''}</button>`;
    return `<section id="vendor-section"><div class="section-head"><div><h1>Рядом с ценой NPC</h1><p>Одна строка на предмет: минимальная цена за штуку в стакане и её отклонение от продажи NPC.</p></div></div>
      <section class="box"><div class="metrics"><div class="metric"><small>Предметов по фильтру</small><strong>${s.items}</strong></div><div class="metric"><small>Дешевле NPC / ровно NPC</small><strong>${s.below_npc} / ${s.equal_npc}</strong></div><div class="metric"><small>Дороже NPC</small><strong>${s.above_npc}</strong></div></div>
      <p>Полный скан обычного аукциона: ${data.snapshot?c.date(data.snapshot.observed_at):'ещё нет'}.</p>
      <p class="note">Наценка = (минимальная цена аука / цена продажи NPC − 1) × 100%. Например: 1 золото 3 меди против 1 золота NPC — это +0,03%. Отрицательное значение означает цену ниже NPC. Минимум учитывает все предложения, включая свои.</p></section>
      <form id="vendor-filters" class="box filters vendor-floor-filters"><label>Предмет или ID<input name="q" maxlength="100" value="${h(f.q)}"></label>
      <label>Наценка от, %<input name="min_markup" aria-label="Наценка от" type="number" min="-100" max="1000000" step="0.0001" value="${h(f.min_markup)}" placeholder="Без ограничения"></label>
      <label>Наценка до, %<input name="max_markup" aria-label="Наценка до" type="number" min="-100" max="1000000" step="0.0001" value="${h(f.max_markup)}" placeholder="Без ограничения"><span class="note">Пустое поле — без ограничения</span></label>
      <label>Сортировка<select name="sort">${Object.entries(titles).map(([v,n])=>`<option value="${v}" ${v===f.sort?'selected':''}>${n}</option>`).join('')}</select></label>
      <label>Порядок<select name="order"><option value="asc" ${f.order==='asc'?'selected':''}>По возрастанию</option><option value="desc" ${f.order==='desc'?'selected':''}>По убыванию</option></select></label>
      <label>Возраст скана, часы<input name="age" type="number" min="0" max="8760" step="0.25" value="${f.max_age_seconds/3600}"><span class="note">0 — без ограничения</span></label><label class="check" title="Одиночные продажи стакаемых материалов остаются, если стакаемость известна. Предметы без данных о стакаемости скрываются."><input name="stackable_only" type="checkbox" ${f.stackable_only?'checked':''}>Скрыть нестакаемые</label><div class="actions"><button type="submit">Применить</button><button type="button" data-vendor-reset>Сбросить</button></div></form>
      <section class="box"><div class="table-wrap"><table class="vendor-floor-table"><thead><tr><th>${header('name','Предмет')}</th><th class="numeric">${header('price','Минимум аука / шт.')}</th><th class="numeric">${header('vendor','Продажа NPC / шт.')}</th><th class="numeric">${header('markup','Наценка к NPC')}</th><th class="numeric">Разница / шт.</th><th class="numeric">${header('quantity','Предложение, шт.')}</th></tr></thead><tbody>${data.items.map(r=>`<tr><td>${h(r.special_variant?r.variant_name:r.name)}<div class="note">#${r.item_id}${r.special_variant?' · минимум особого варианта':''}<br>${c.date(r.observed_at)}</div>${(Date.parse(data.as_of)-Date.parse(r.observed_at))/1000>data.stale_after_seconds?'<span class="badge pending">Скан устарел</span>':''}${r.owned_at_min?'<div class="note">Минимум — ваше предложение</div>':''}</td><td class="numeric"><strong>${this.unit(r.min_unit_copper)}</strong></td><td class="numeric">${this.unit(r.vendor_sell_copper)}<div class="note">${h(r.vendor_source)}${r.vendor_phase==='beta'?' · бета':''}</div></td><td class="numeric"><span class="badge ${r.markup_percent>0?'pending':''}">${this.percent(r.markup_percent)}</span></td><td class="numeric">${r.difference_copper>0?'+':''}${this.unit(r.difference_copper)}</td><td class="numeric" title="Сумма всех предложений предмета, включая другие варианты">${r.available_quantity.toLocaleString('ru-RU')}</td></tr>`).join('')||`<tr><td colspan="6">${data.snapshot?'Предметов по этим условиям нет. Измените диапазон наценки.':'Нужен полный скан обычного аукциона.'}</td></tr>`}</tbody></table></div>
      <div class="pagination"><button data-vendor-offset="${Math.max(0,data.offset-data.limit)}" ${data.offset===0?'disabled':''}>Назад</button><span>${data.total?data.offset+1:0}–${Math.min(data.offset+data.items.length,data.total)} из ${data.total}</span><button data-vendor-offset="${data.offset+data.limit}" ${data.offset+data.limit>=data.total?'disabled':''}>Далее</button></div></section></section>`;
  },
  async poll(){
    if(!document.getElementById('vendor-section')||this.polling||Date.now()-(this.lastPoll||0)<10000||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement?.tagName))return;
    this.polling=true;this.lastPoll=Date.now();
    try{const query=this.query().toString(),before=this.data?.view_cache?.updated_at,data=await this.context.api('/vendor-bargains?'+query);
      if(document.getElementById('vendor-section')&&query===this.query().toString()&&before!==data.view_cache?.updated_at){this.data=data;await this.context.open('vendor');}
    }finally{this.polling=false;}
  },
  bind(context){
    this.context=context;this.preferenceKey='cennik.npc-floor.v2:'+(window.CennikPrefix||'local');
    try{const saved=JSON.parse(localStorage.getItem(this.preferenceKey)||'null');if(saved&&typeof saved==='object')for(const key of Object.keys(this.defaults))if(key in saved)this.filters[key]=saved[key];}catch{}
    document.addEventListener('input',event=>{if(event.target.form?.id==='vendor-filters')event.target.form.elements.min_markup.setCustomValidity('');});
    document.addEventListener('change',event=>{if(event.target.name==='stackable_only'&&event.target.form?.id==='vendor-filters')event.target.form.requestSubmit();});
    document.addEventListener('submit',async event=>{
      if(event.target.id!=='vendor-filters')return;event.preventDefault();const f=new FormData(event.target);
      if(f.get('min_markup')!==''&&f.get('max_markup')!==''&&Number(f.get('min_markup'))>Number(f.get('max_markup'))){const input=event.target.elements.min_markup;input.setCustomValidity('Наценка «от» должна быть не больше значения «до».');input.reportValidity();return;}
      this.filters={q:String(f.get('q')).trim(),sort:String(f.get('sort')),order:String(f.get('order')),min_markup:String(f.get('min_markup')),max_markup:String(f.get('max_markup')),max_age_seconds:Math.round(Number(f.get('age'))*3600),stackable_only:f.has('stackable_only')};
      this.offset=0;this.save();try{await context.open('vendor');}catch(error){context.notify(error.message,true);}
    });
    document.addEventListener('click',async event=>{
      const button=event.target.closest('[data-vendor-offset],[data-vendor-sort],[data-vendor-reset]');if(!button||button.disabled)return;
      if(button.hasAttribute('data-vendor-reset')){this.filters={...this.defaults};this.offset=0;}
      else if(button.dataset.vendorSort){const key=button.dataset.vendorSort;this.filters.order=this.filters.sort===key&&this.filters.order==='asc'?'desc':'asc';this.filters.sort=key;this.offset=0;}
      else this.offset=Number(button.dataset.vendorOffset);
      this.save();try{await context.open('vendor');}catch(error){context.notify(error.message,true);}
    });
  }
};
