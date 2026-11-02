"use strict";
window.CennikBulk={
  selected:new Set(),chosen:new Set(),quantities:{},batch:null,detail:null,context:null,
  status(value){return {complete:'Проверено',partial:'Неполная проверка',cancelled:'Остановлено',awaiting_scan:'Ожидает проверки в игре'}[value]||value;},
  cell(o){return `<input type="checkbox" aria-label="Проверить ${this.context.h(o.item_name)}" data-bulk-select="${this.context.h(o.id)}" ${this.selected.has(o.id)?"checked":""}>`;},
  toolbar(){return `<section class="box"><h2>Массовая проверка в игре</h2><p>Отметьте возможности на любых страницах. Общие компоненты проверяются один раз. Предел: 200 возможностей и 200 уникальных предметов.</p><div class="actions"><button data-bulk-action="page">Выбрать страницу</button><label>Первые <input id="bulk-first-count" type="number" value="50" min="1" max="200" aria-label="Количество для массовой проверки"></label><button data-bulk-action="first">Выбрать первые</button><button data-bulk-action="clear">Снять выбор</button><button class="primary" data-bulk-action="send">Отправить на проверку (<span id="bulk-selection-count">${this.selected.size}</span>)</button><button data-page="bulk">Результаты проверок</button><button data-prod-action="from-opportunities">Производственный заказ</button></div><p class="note">После отправки: /reload → «Проверка из веба» в аддоне → «Проверить пакет» → /reload → синхронизация. Это отдельный этап, без покупок.</p></section>`;},
  async render(){
    const c=this.context,h=c.h,m=c.money;
    const listing=await c.api('/bulk');
    if(!listing.items.length)return `<h1>Массовая проверка</h1><section class="box"><p>Пакетов пока нет. Отметьте возможности и отправьте их на проверку в игре.</p><button data-page="opportunities">Выбрать возможности</button></section>`;
    if(!this.batch||!listing.items.some(x=>x.batch_id===this.batch))this.batch=listing.items[0].batch_id;
    const data=await c.api('/bulk/'+this.batch);
    const changed=data.items.filter(x=>this.quantities[x.opportunity_id]).map(x=>({opportunity_id:x.opportunity_id,batches:this.quantities[x.opportunity_id]}));
    for(let offset=0;offset<changed.length;offset+=100){const evaluated=await c.post('/bulk/'+this.batch+'/evaluate',{choices:changed.slice(offset,offset+100)});const updates=new Map(evaluated.items.map(x=>[x.opportunity_id,x]));data.items=data.items.map(x=>updates.get(x.opportunity_id)||x);}
    this.detail=data;
    for(const row of data.items)if(!row.complete||row.profit_copper<=0||!data.current)this.chosen.delete(row.opportunity_id);
    const report=data.report;
    return `<h1>Массовая проверка</h1><section class="box stack"><label>Пакет <select id="bulk-batch">${listing.items.map(x=>`<option value="${h(x.batch_id)}" ${x.batch_id===this.batch?'selected':''}>${h(c.date(x.created_at*1000))} · ${x.candidates} возможностей / ${x.unique_items} предметов · ${h(this.status(x.status))}</option>`).join('')}</select></label><p>Проверка цен и доступного объёма отдельно от рабочих заданий. Расчёты строк независимы; при общей отправке компоненты распределяются совместно, общий бюджет проверяется повторно.</p><div class="actions"><button data-action="sync">Загрузить результаты из игры</button><button data-bulk-action="refresh">Обновить</button><button data-page="opportunities">Выбрать ещё</button></div>${!data.current?'<div class="notice">Изменился исходный расчёт. Для отправки рабочих заданий создайте новый пакет проверки.</div>':''}<p>${report?`Отчёт: ${h(this.status(report.status))} · ${h(c.date(report.completed_at*1000))} · ${h(report.reason||'')}`:'Ожидается проверка: /reload в игре, подтвердите рынок и нажмите «Проверить пакет».'}</p><p class="note">Предложения старше 30 минут не используются для отправки. NPC-цены и наличие не подтверждаются этим сканом. Объём закупки не гарантирует сбыт.</p></section>
      <section class="box"><div class="actions"><button data-bulk-action="profitable">Выбрать прибыльные</button><button data-bulk-action="evaluate">Уточнить выбранные объёмы</button><button class="primary" data-bulk-action="publish">Передать выбранные в рабочие задания</button></div><p class="note">До 100 рабочих заданий за отправку. Поле «Партии» — число исходных единиц расчёта/крафтов. «В бюджете» — предел закупки, не рекомендация сбыта. NPC-запас неизвестен.</p><p class="note">Таблица прокручивается по горизонтали.</p><div class="table-wrap"><table class="bulk-results"><thead><tr><th>Выбор / возможность</th><th>Партии</th><th>Доступно партий</th><th>В бюджете</th><th>Закупка</th><th>Прибыль</th></tr></thead><tbody>${data.items.map(o=>{
        const positive=o.complete&&o.profit_copper>0&&data.current;
        return `<tr><td><label><input type="checkbox" data-bulk-choice="${h(o.opportunity_id)}" ${this.chosen.has(o.opportunity_id)?'checked':''} ${positive?'':'disabled'}>${h(o.item_name)}</label><div class="note">${h(c.labels[o.strategy_id])}</div><details><summary>${o.complete?'Состав закупки и ограничения':'Неполная проверка'}</summary><p>${h(o.problems.join('; '))}</p><p>${h(o.warnings.join('; '))}</p>${(o.materials||[]).map(r=>`<p>#${r.item_id} × ${r.quantity} · ${h(r.source)} · ${m(r.cost_copper)}${r.offers?` · точных строк ${r.offers.length}`:''}</p>`).join('')}${c.graphHtml?.(o.graph,false)||""}<p>${o.oldest_observed_at?'Самое раннее наблюдение: '+h(c.date(o.oldest_observed_at*1000)):''}</p></details></td><td><input type="number" min="1" max="10000" value="${this.quantities[o.opportunity_id]||o.batches}" data-bulk-quantity="${h(o.opportunity_id)}" aria-label="Партии ${h(o.item_name)}"></td><td>${o.available_batches??'неизвестно'}</td><td>${o.budget_batches??'—'}${o.at_budget?`<div class="note">Прибыль: ${m(o.at_budget.profit_copper)}</div>`:''}</td><td data-bulk-cost="${o.cost_copper??''}">${m(o.cost_copper)}</td><td data-bulk-profit="${o.profit_copper??''}">${m(o.profit_copper)}</td></tr>`;
      }).join('')}</tbody></table></div><p>Отправлено рабочих пакетов из этой проверки: ${data.publications.length}.</p></section>`;
  },
  bind(c){this.context=c;const b=this;
    document.addEventListener('change',async e=>{try{
      if(e.target.dataset.bulkSelect){e.target.checked?b.selected.add(e.target.dataset.bulkSelect):b.selected.delete(e.target.dataset.bulkSelect);const n=document.getElementById('bulk-selection-count');if(n)n.textContent=b.selected.size;}
      if(e.target.dataset.bulkChoice)e.target.checked?b.chosen.add(e.target.dataset.bulkChoice):b.chosen.delete(e.target.dataset.bulkChoice);
      if(e.target.dataset.bulkQuantity){const count=Number(e.target.value);if(!Number.isInteger(count)||count<1||count>10000)throw Error('Количество партий: от 1 до 10000');b.quantities[e.target.dataset.bulkQuantity]=count;}
      if(e.target.id==='bulk-batch'){b.batch=e.target.value;b.chosen.clear();b.quantities={};await c.open('bulk');}
    }catch(error){c.notify(error.message,true);}});
    document.addEventListener('click',async e=>{const button=e.target.closest('[data-bulk-action]');if(!button)return;try{
      const action=button.dataset.bulkAction;
      if(action==='page'){for(const o of c.items())b.selected.add(o.id);await c.open('opportunities');}
      if(action==='clear'){b.selected.clear();await c.open('opportunities');}
      if(action==='first'){const count=Number(document.getElementById('bulk-first-count').value);if(!Number.isInteger(count)||count<1||count>200)throw Error('Выберите от 1 до 200 возможностей');const query=c.query();const result=await c.api('/analytics/opportunities?limit='+count+'&offset=0&'+query);b.selected=new Set(result.items.map(x=>x.id));await c.open('opportunities');}
      if(action==='send'){if(!b.selected.size||b.selected.size>200)throw Error('Выберите от 1 до 200 возможностей');await c.job('/bulk',{opportunity_ids:[...b.selected]},'Пакет проверки отправляется в аддон. После завершения выполните /reload');b.batch=null;b.chosen.clear();b.quantities={};await c.open('bulk');}
      if(action==='refresh')await c.open('bulk');
      if(action==='profitable'){b.chosen=new Set(b.detail.current?b.detail.items.filter(x=>x.complete&&x.profit_copper>0).slice(0,100).map(x=>x.opportunity_id):[]);await c.open('bulk');}
      if(action==='evaluate'){
        const choices=b.detail.items.filter(x=>b.chosen.has(x.opportunity_id)||b.quantities[x.opportunity_id]).map(x=>({opportunity_id:x.opportunity_id,batches:b.quantities[x.opportunity_id]||1}));
        if(!choices.length)throw Error('Выберите строки или измените объёмы');
        await c.open('bulk');
      }
      if(action==='publish'){
        if(!b.chosen.size||b.chosen.size>100)throw Error('Выберите от 1 до 100 проверенных возможностей');
        const choices=[...b.chosen].map(id=>({opportunity_id:id,batches:b.quantities[id]||1}));
        await c.job('/bulk/'+b.batch+'/publish',{choices,request_id:crypto.randomUUID().replaceAll('-','')},'Выбранные возможности передаются в обычные задания аддона. После завершения выполните /reload');await c.open('bulk');
      }
    }catch(error){c.notify(error.message,true);}});
  }
};
