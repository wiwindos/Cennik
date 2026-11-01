"use strict";
const {chromium}=require(process.argv[2]);
const assert=require("node:assert/strict");
const path=require("node:path");
const legacyMode=process.argv[5]==="npc-legacy";
const catalogMode=process.argv[5]==="npc-catalog"||legacyMode;
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];
  page.on("pageerror",e=>errors.push(String(e)));
  page.on("response",r=>{if(r.status()>=400)errors.push(r.status()+" "+r.url());});
  try{
    if(legacyMode)await page.route('**/api/v2/overview',async route=>{
      const response=await route.fetch();const value=await response.json();delete value.npc_catalog;
      await route.fulfill({response,json:value});
    });
    await page.goto("http://127.0.0.1:8767");
    await page.locator("#strategy-filter").selectOption("npc_to_auction");
    if(legacyMode){
      await page.getByRole('heading',{name:'Нужен перезапуск Cennik'}).waitFor();
      await page.unroute('**/api/v2/overview');
      await page.reload();
      await page.locator('#strategy-filter').selectOption('npc_to_auction');
      await page.getByRole('heading',{name:'Стратегия NPC → аукцион выключена'}).waitFor();
      await page.getByRole('button',{name:'Включить стратегию и пересчитать'}).click();
    }
    await page.locator("[data-opportunity]").first().click();
    await page.getByText("Наличие, запас, доступ к торговцу",{exact:false}).waitFor();
    if(catalogMode)await page.getByText("Условный кандидат: buy API принят",{exact:false}).first().waitFor();
    await page.locator("#batches").fill("5");
    await page.getByRole("button",{name:"Рассчитать по объёму"}).click();
    await page.getByText("Закупка по справочной цене NPC",{exact:false}).waitFor();
    const plan=await page.evaluate(async()=>await (await fetch('/api/v2/plans')).json());
    assert.equal(plan.items[0].cost_copper,catalogMode?275:100);
    assert.equal(plan.items[0].lines[0].source,"npc");
    assert.equal(plan.items[0].npc_stock_confirmed,false);
    assert.equal(plan.items[0].profit_copper,catalogMode?205:380);
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      await page.screenshot({path:path.join(process.argv[4],`npc-resale-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"Page overflow at "+width);
    }
    await page.locator('nav [data-page="settings"]').click();
    assert(await page.locator('[data-strategy="npc_to_auction"]').isChecked());
    await page.locator('nav [data-page="sources"]').click();
    await page.getByRole("heading",{name:"Цены покупки у NPC"}).waitFor();
    if(catalogMode){
      await page.getByText("Бета-справочник: актуализировать перед релизом.",{exact:false}).waitFor();
      const before=await page.evaluate(async()=>await(await fetch('/api/v2/overview')).json());
      const doc=await page.evaluate(async()=>await(await fetch('/api/v2/npc/catalog/document')).json());
      doc.version="ui-npc-live-2";doc.phase="release";doc.items["2589"].buy_copper=40;
      await page.locator('#npc-catalog-file').setInputFiles({name:'fixture-release.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(doc))});
      await page.locator('#npc-catalog-preview').getByText('Данные релиза.',{exact:false}).waitFor();
      await page.getByRole('button',{name:'Применить справочник NPC',exact:true}).click();
      await page.getByText('Версия ui-npc-live-2',{exact:false}).waitFor();
      await page.getByText('Справочник релиза. Актуальную цену',{exact:false}).waitFor();
      const after=await page.evaluate(async()=>await(await fetch('/api/v2/overview')).json());
      assert.notEqual(before.generation,after.generation);
      assert.equal(after.npc_catalog.requires_release_refresh,false);
      for(const width of [1440,390]){
        await page.setViewportSize({width,height:1000});
        await page.screenshot({path:path.join(process.argv[4],`npc-catalog-sources-${width}.png`),fullPage:true});
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Sources overflow');
      }
    }
    assert.deepEqual(errors,[]);
    console.log("NPC resale UI passed: strategy, source/stock warnings, party economics, settings, two widths, no browser errors");
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
