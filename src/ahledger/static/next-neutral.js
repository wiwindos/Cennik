"use strict";
window.CennikNeutral={
  filters:{mode:"deals",q:"",min_profit_copper:0,min_roi:0,max_roi:"",budget_copper:"",max_age_seconds:0,sort:"profit",order:"desc",min_sales:"",max_sales:"",price_basis:"buy",min_price_copper:"",max_price_copper:""},
  offset:0,selected:null,lastPoll:0,polling:false,
  flags:{unknown_sales:"Спрос неизвестен",no_history:"Нет исторического ориентира",no_variant_price:"Нет цены точного варианта",no_sale_price:"Нет цены продажи",exact_variant:"Точный вариант"},
  query(extra={}){const values={...this.filters,offset:this.offset,limit:50,...extra};return new URLSearchParams(Object.entries(values).filter(([,v])=>v!==""&&v!==null)).toString();},
  async render(){
    const c=this.context,h=c.h,f=this.filters;
    try{this.data=await c.api('/neutral?'+this.query());}
    catch(error){if(!error.viewPending)throw error;this.data={pending:true,items:[],summary:{},total:0,offset:this.offset,limit:50};this.retry();}
    if(this.data.comparison_refreshing)this.retry(2000);
    this.lastPoll=Date.now();
    return `<section id="neutral-section"><div class="section-head"><div><h1>Межфракционный аукцион</h1><p>Покупка здесь → продажа на обычном аукционе. Расчёт обновляется с новыми данными.</p></div></div>
      <div id="neutral-summary">${this.summary(this.data)}</div>
      <section class="box"><div class="actions neutral-tabs" role="group" aria-label="Список предметов"><button data-neutral-mode="deals" class="${f.mode==='deals'?'primary':''}">Выгодные сделки</button><button data-neutral-mode="all" class="${f.mode==='all'?'primary':''}">Все предметы</button></div>
      <form id="neutral-filters" class="fields"><label>Название или ID<input name="q" maxlength="100" value="${h(f.q)}" placeholder="Поиск предмета"></label>
      <label>Сортировать<select name="sort">${[['profit','Прибыль всей партии'],['roi','Доходность, %'],['unit','Прибыль за штуку'],['sales','Продажи в сутки'],['buy_price','Цена покупки за штуку'],['sale_price','Ориентир продажи за штуку']].map(([v,n])=>`<option value="${v}" ${f.sort===v?'selected':''}>${n}</option>`).join('')}</select></label>
      <label>Порядок<select name="order"><option value="desc" ${f.order==='desc'?'selected':''}>Сначала больше</option><option value="asc" ${f.order==='asc'?'selected':''}>Сначала меньше</option></select></label>
      <label>Продажи в сутки<div class="neutral-range"><input name="min_sales" aria-label="Продажи в сутки от" type="number" min="0" max="1000000000000" step="any" value="${f.min_sales}" placeholder="От"><input name="max_sales" aria-label="Продажи в сутки до" type="number" min="0" max="1000000000000" step="any" value="${f.max_sales}" placeholder="До"></div></label>
      <label>Цена для фильтра<select name="price_basis"><option value="buy" ${f.price_basis==='buy'?'selected':''}>Покупка от / шт.</option><option value="sale" ${f.price_basis==='sale'?'selected':''}>Ориентир продажи / шт.</option></select></label>
      <label>Цена за штуку, золото<div class="neutral-range"><input name="min_price" aria-label="Цена от" type="number" min="0" max="900719925474" step="0.0001" value="${f.min_price_copper===''?'':f.min_price_copper/10000}" placeholder="От"><input name="max_price" aria-label="Цена до" type="number" min="0" max="900719925474" step="0.0001" value="${f.max_price_copper===''?'':f.max_price_copper/10000}" placeholder="До"></div></label>
      <label>Минимальная прибыль, золото<input name="profit" type="number" min="0" step="0.0001" max="900719925474" value="${f.min_profit_copper/10000}"></label>
      <label>Доходность, %<div class="neutral-range"><input name="roi" aria-label="Доходность от" type="number" min="0" max="1000000" step="0.1" value="${f.min_roi}" placeholder="От"><input name="max_roi" aria-label="Доходность до" type="number" min="0" max="1000000" step="0.1" value="${f.max_roi}" placeholder="До"></div></label>
      <label>Бюджет на один предмет, золото<input name="budget" type="number" min="0" max="900719925474" step="0.0001" value="${f.budget_copper===''?'':f.budget_copper/10000}" placeholder="Без ограничения"></label>
      <label>Максимальный возраст обеих цен, часы<input name="age" type="number" min="0" max="8760" step="0.25" value="${f.max_age_seconds/3600}"><span class="note">0 — показывать и старые, с отметкой времени</span></label>
      <div class="actions"><button type="submit">Применить</button><button type="button" data-neutral-reset>Сбросить</button></div></form>
      <p class="note">Бюджет применяется отдельно к каждой строке. Партия состоит из целых лотов; предложения не резервируются. Свои лоты исключены. В «Все предметы» входят предложения с выкупом. При фильтре по продажам неизвестная ликвидность исключается; без фильтра остаётся видимой. При сортировке неизвестные значения всегда в конце.</p>
      <div id="neutral-results">${this.table(this.data)}</div></section><div id="neutral-detail" aria-live="polite"></div></section>`;
  },
  summary(data){
    const c=this.context,h=c.h,s=data.summary,head=s.snapshot;
    if(data.pending)return '<section class="box"><p role="status">Загружаем сравнение в фоне. Поиск и фильтры уже доступны.</p></section>';
    const partial=s.last_attempt&&!s.last_attempt.complete;
    return `<section class="box"><div class="metrics"><div class="metric"><small>Последний полный межфракционный скан</small><strong>${head?h(Number(s.row_count).toLocaleString('ru-RU'))+' лотов':'Ещё нет'}</strong><p>${head?c.date(head.observed_at):'Откройте подтверждённый межфракционный аукцион, дождитесь полного скана и выполните /reload.'}</p></div><div class="metric"><small>Предметы / варианты с выкупом</small><strong>${s.item_count}</strong><p>Сохранено ${s.scans} сканов, полных ${s.complete_scans}</p></div><div class="metric"><small>Комиссия рынка продажи</small><strong>${h(data.cut_percent)}%</strong><p>${h(data.sale_market)}</p></div></div>
      <p>Цены обычного рынка: ${c.date(s.ordinary_observed_at)}. ${partial?'Последняя попытка частичная — используем предыдущий полный снимок.':''}</p>
      ${data.comparison_refreshing?'<p class="notice">Рынок обновляется. Показано последнее готовое сравнение с исходными датами цен.</p>':''}
      <p class="note">Ориентир продажи — меньшая из доступных текущей минимальной цены, медианы и исторической медианы AHledger 30d. Для особых вариантов нужна цена того же варианта из собственного скана. Ликвидность AHledger для них показывается по предмету в целом, для всех вариантов. Продажа по этой цене не подтверждена. Депозит при неудачных выставлениях и время переноса не учтены.</p></section>`;
  },
  title(row){return row.special_variant?row.variant_name:(row.name||'Предмет');},
  liquidity(row,data){
    const c=this.context,h=c.h,value=Number(row.sales_per_day);
    if(row.sales_per_day===null||row.sales_per_day===undefined||!Number.isFinite(value))return `<div class="note neutral-liquidity">Ликвидность: неизвестна<br>${row.sales_unavailable_reason==='own_data_only'?'Включён режим только собственных данных':'AHledger не передал оценку продаж'}</div>`;
    const amount=value>0&&value<0.01?'&lt;0,01':h(value.toLocaleString('ru-RU',{maximumFractionDigits:2}));
    const source=row.sales_source==='AHL1'?'AHledger':(row.sales_source||'Источник не указан');
    const seconds=(Date.parse(data.as_of)-Date.parse(row.sales_observed_at))/1000;
    const stale=Number.isFinite(seconds)&&seconds>data.stale_after_seconds;
    return `<div class="note neutral-liquidity">Ликвидность: ≈ ${amount} продаж/сутки${row.sales_scope==='all_variants'?'<br>По предмету · все варианты':''}<br>${h(source)} · ${row.sales_observed_at?c.date(row.sales_observed_at):'дата оценки неизвестна'}${stale?'<br><span class="badge pending">Оценка устарела</span>':''}</div>`;
  },
  ages(row){const c=this.context;return `<div class="note">Межфракционный: ${c.date(this.data.summary.snapshot?.observed_at)}<br>Обычный: ${c.date(row.sale_observed_at)}</div>`;},
  table(data){
    if(data.pending)return '<p class="note" role="status">Подготавливаем цены и ликвидность…</p>';
    const c=this.context,h=c.h,m=c.money;
    const reason=r=>r.sale_copper===null?(r.special_variant?'Нет цены точного варианта':'Нет цены продажи'):r.quantity===0?(r.profit_per_unit_copper<=0?'Нет прибыли после комиссии':'Целый лот не помещается в бюджет'):'';
    const stale=r=>[r.sale_age_seconds,r.neutral_age_seconds].some(a=>a!==null&&a>data.stale_after_seconds);
    return `<div class="table-wrap"><table class="neutral-table"><thead><tr><th>Предмет / свежесть</th><th class="numeric">Покупка от / шт.</th><th class="numeric">Ориентир продажи / шт.</th><th class="numeric">Купить / доступно</th><th class="numeric">Затраты</th><th class="numeric">Чистая прибыль</th><th class="numeric">Доходность</th></tr></thead><tbody>${data.items.map(r=>`<tr><td><button class="link" data-neutral-item="${r.id}" aria-expanded="${this.selected===r.id}">${h(this.title(r))}</button><div class="note">#${r.item_id}${r.special_variant?' · особый вариант':''}</div>${this.ages(r)}${(r.flags||[]).includes('unknown_sales')?'<span class="badge pending">Спрос неизвестен</span>':''}${stale(r)?'<span class="badge pending">Данные устарели</span>':''}${reason(r)?`<div class="note">${h(reason(r))}</div>`:''}</td>
        <td class="numeric">${m(r.buy_min_copper)}</td><td class="numeric">${m(r.sale_copper)}<div class="note">Мин.: ${m(r.ordinary_min_copper)}<br>Медиана: ${m(r.ordinary_median_copper)}<br>История: ${m(r.historical_median_copper)}</div>${this.liquidity(r,data)}</td>
        <td class="numeric">${r.quantity} / ${r.quantity_available}<div class="note">${r.selected_lot_count} целых лотов</div></td><td class="numeric">${r.quantity?m(r.cost_copper):'—'}</td><td class="numeric">${r.quantity?`<strong>${m(r.profit_copper)}</strong><div class="note">${m(r.profit_per_unit_copper)} / шт.</div>`:'—'}</td><td class="numeric">${r.roi_percent===null?'—':r.roi_percent.toFixed(1)+'%'}</td></tr>`).join('')||`<tr><td colspan="7">${data.summary.snapshot?'По выбранным условиям предметов нет. Измените фильтры или откройте «Все предметы».':'Ждём первый полный межфракционный скан. Частичные и неопределённые снимки не создают сделки.'}</td></tr>`}</tbody></table></div>
      <div class="pagination"><button data-neutral-offset="${Math.max(0,data.offset-data.limit)}" ${data.offset===0?'disabled':''}>Назад</button><span>${data.total?data.offset+1:0}–${Math.min(data.offset+data.items.length,data.total)} из ${data.total}</span><button data-neutral-offset="${data.offset+data.limit}" ${data.offset+data.limit>=data.total?'disabled':''}>Далее</button></div>`;
  },
  async details(){
    const target=document.getElementById('neutral-detail');if(!target||!this.selected)return;
    const selected=this.selected,c=this.context,h=c.h,m=c.money;
    let data;
    try{data=await c.api('/neutral?'+this.query({mode:'all',q:'',selected,offset:0,limit:50,min_profit_copper:0,min_roi:0,max_roi:'',max_age_seconds:0,min_sales:'',max_sales:'',min_price_copper:'',max_price_copper:''}));}
    catch(error){if(!error.viewPending)throw error;if(this.selected===selected&&target.isConnected){target.innerHTML='<section class="box" role="status">Загружаем расчёт предмета…</section>';clearTimeout(this.detailRetry);this.detailRetry=setTimeout(()=>{if(this.selected===selected&&target.isConnected)this.details().catch(e=>c.notify(e.message,true));},750);}return;}
    if(this.selected!==selected||!target.isConnected)return;
    const r=data.items[0];if(!r){target.innerHTML='<section class="box">Предмета больше нет в последнем полном снимке.</section>';return;}
    for(const b of document.querySelectorAll('[data-neutral-item]')){b.setAttribute('aria-expanded',String(b.dataset.neutralItem===selected));b.closest('tr').classList.toggle('selected',b.dataset.neutralItem===selected);}
    target.innerHTML=`<section class="box stack"><div class="section-head"><div><h2>${h(this.title(r))} · #${r.item_id}</h2><p>Расчёт по данным снимка, без отправки в аддон.</p></div><button data-neutral-close>Свернуть</button></div>
      <div class="metrics"><div class="metric"><small>Закупка ${r.quantity} шт.</small><strong>${m(r.cost_copper)}</strong></div><div class="metric"><small>После комиссии ${h(data.cut_percent)}%</small><strong>${m(r.net_copper)}</strong></div><div class="metric"><small>Ожидаемая прибыль</small><strong>${m(r.profit_copper)}</strong></div></div>
      <p>Продажа за штуку: ${m(r.sale_copper)} · ${h(r.sale_source||'нет сопоставимой цены')} · ${c.date(r.sale_observed_at)}.</p>${this.liquidity(r,data)}<p class="note">Продажа не подтверждена.</p>
      <p class="note">${r.flags.filter(f=>this.flags[f]).map(f=>h(this.flags[f])).join(' · ')}. Затраты ${m(r.cost_copper)} + комиссия ${m(r.cut_copper)} + прибыль ${m(r.profit_copper)} = выручка ${m(r.gross_copper)}.</p>
      <div class="table-wrap"><table><thead><tr><th>Лот в снимке</th><th>Количество</th><th>Выкуп целиком</th><th>Выручка после комиссии</th><th>Прибыль</th></tr></thead><tbody>${r.selected_lots.map(l=>`<tr><td>#${l.sequence+1}</td><td>${l.quantity}</td><td>${m(l.buyout_copper)}</td><td>${m(l.net_copper)}</td><td>${m(l.profit_copper)}</td></tr>`).join('')||'<tr><td colspan="5">Подходящих целых лотов для закупки нет.</td></tr>'}</tbody></table></div>${r.lots_truncated?`<p>Показаны первые 50 из ${r.selected_lot_count} лотов. Итоги включают всю партию.</p>`:''}</section>`;
  },
  retry(delay=750){const query=this.query();clearTimeout(this.retryTimer);this.retryTimer=setTimeout(()=>{if(document.getElementById('neutral-section')&&query===this.query())this.poll(true).catch(e=>this.context.notify(e.message,true));},delay);},
  async poll(force=false){
    if(!document.getElementById('neutral-section')||this.polling||!force&&Date.now()-this.lastPoll<10000)return;
    this.polling=true;this.lastPoll=Date.now();
    const query=this.query();
    try{
      const data=await this.context.api('/neutral?'+query);
      if(!document.getElementById('neutral-section')||query!==this.query())return;
      this.data=data;document.getElementById('neutral-summary').innerHTML=this.summary(data);
      document.getElementById('neutral-results').innerHTML=this.table(data);if(this.selected)await this.details();
      if(data.comparison_refreshing)this.retry(2000);
    }catch(error){if(!error.viewPending)throw error;if(query===this.query())this.retry();}
    finally{this.polling=false;}
  },
  bind(context){
    this.context=context;
    document.addEventListener('submit',async e=>{
      if(e.target.id!=='neutral-filters')return;e.preventDefault();
      const f=new FormData(e.target);
      const optional=(name,multiplier=1)=>f.get(name)===''?'':Math.round(Number(f.get(name))*multiplier*1000000)/1000000;
      this.filters={...this.filters,q:String(f.get('q')).trim(),sort:String(f.get('sort')),order:String(f.get('order')),min_profit_copper:Math.round(Number(f.get('profit'))*10000),min_roi:Number(f.get('roi')),max_roi:optional('max_roi'),budget_copper:f.get('budget')===''?'':Math.round(Number(f.get('budget'))*10000),max_age_seconds:Math.round(Number(f.get('age'))*3600),min_sales:optional('min_sales'),max_sales:optional('max_sales'),price_basis:String(f.get('price_basis')),min_price_copper:f.get('min_price')===''?'':Math.round(Number(f.get('min_price'))*10000),max_price_copper:f.get('max_price')===''?'':Math.round(Number(f.get('max_price'))*10000)};
      this.offset=0;this.selected=null;
      try{await context.open('neutral');}catch(error){context.notify(error.message,true);}
    });
    document.addEventListener('click',async e=>{
      const b=e.target.closest('[data-neutral-mode],[data-neutral-item],[data-neutral-offset],[data-neutral-close],[data-neutral-reset]');if(!b)return;
      try{
        if(b.hasAttribute('data-neutral-close')){this.selected=null;document.getElementById('neutral-detail').innerHTML='';for(const button of document.querySelectorAll('[data-neutral-item]')){button.setAttribute('aria-expanded','false');button.closest('tr').classList.remove('selected');}return;}
        if(b.dataset.neutralItem){this.selected=b.dataset.neutralItem;await this.details();document.getElementById('neutral-detail')?.scrollIntoView({block:'nearest',behavior:'smooth'});return;}
        if(b.hasAttribute('data-neutral-reset'))this.filters={...this.filters,q:'',min_profit_copper:0,min_roi:0,max_roi:'',budget_copper:'',max_age_seconds:0,sort:'profit',order:'desc',min_sales:'',max_sales:'',price_basis:'buy',min_price_copper:'',max_price_copper:''};
        if(b.dataset.neutralMode)this.filters.mode=b.dataset.neutralMode;
        this.offset=b.dataset.neutralOffset===undefined?0:Number(b.dataset.neutralOffset);this.selected=null;
        await context.open('neutral');
      }catch(error){context.notify(error.message,true);}
    });
  }
};
// EOF
