"use strict";
const {chromium}=require(process.argv[2]);
const assert=require("node:assert/strict"),path=require("node:path");
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}});const errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  try{
    await page.goto('http://127.0.0.1:8767');
    await page.locator('#strategy-filter').selectOption('npc_to_auction');
    await page.locator('[data-bulk-select]').first().check();
    await page.locator('[data-bulk-action="send"]').click();
    await page.getByText('Ожидается проверка:',{exact:false}).waitFor({timeout:30000});
    let list=await page.evaluate(async()=>await(await fetch('/api/v2/bulk')).json());
    assert.equal(list.items[0].unique_items,1);
    const batch=list.items[0].batch_id;
    let tasks=await page.evaluate(async()=>await(await fetch('/api/v2/addon/packages?market=forever.pvp.alliance.us')).json());
    assert.equal(tasks.packages.length,0,'Research export must not create working tasks');
    await page.evaluate(async()=>{const r=await fetch('/fixture/game-reload',{method:'POST'});if(!r.ok)throw Error(await r.text());});
    await page.getByRole('button',{name:'Загрузить результаты из игры',exact:true}).click();
    await page.locator('[data-bulk-choice]:enabled').first().waitFor({timeout:30000});
    const row=page.locator('tr').filter({has:page.locator('[data-bulk-choice]')}).first();
    await row.locator('[data-bulk-quantity]').fill('5');
    await row.locator('[data-bulk-quantity]').dispatchEvent('change');
    await page.locator('[data-bulk-action="evaluate"]').click();
    await page.locator('[data-bulk-profit="375"]').waitFor({timeout:30000});
    const evaluated=await page.evaluate(async batch=>await(await fetch('/api/v2/bulk/'+batch+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choices:[{opportunity_id:document.querySelector('[data-bulk-choice]').dataset.bulkChoice,batches:5}]})})).json(),batch);
    assert.equal(evaluated.items[0].cost_copper,100);assert.equal(evaluated.items[0].profit_copper,375);
    assert.equal(evaluated.items[0].available_batches,null,'NPC stock must stay unknown');
    await page.locator('[data-bulk-choice]').first().check();
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`bulk-results-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Page overflow '+width);
    }
    await page.locator('[data-bulk-action="publish"]').click();
    await page.waitForFunction(async()=>{const x=await(await fetch('/api/v2/overview')).json();const j=x.jobs.find(j=>j.kind==='bulk_publish');return j&&['completed','failed'].includes(j.state);},null,{timeout:30000});
    tasks=await page.evaluate(async()=>await(await fetch('/api/v2/addon/packages?market=forever.pvp.alliance.us')).json());
    assert.equal(tasks.packages.length,1,JSON.stringify(await page.evaluate(async()=>await(await fetch('/api/v2/overview')).json())));
    const task=tasks.packages[0].tasks[0];
    assert.equal(task.format_version,3);assert.equal(task.opportunity.quantity,5);
    assert.equal(task.max_total_cost_copper,100);assert.equal(task.predicted_profit_copper,375);
    assert.equal(task.opportunity.details.bulk_check.batch_id,batch);
    assert.deepEqual(errors,[]);
    console.log('Bulk end-to-end passed: web selection → separate import → Lua scan → SavedVariables → web repricing → existing working task; no purchases; 1440/390');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
