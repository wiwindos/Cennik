"use strict";
const {chromium}=require(process.argv[2]);
const assert=require("node:assert/strict");
const path=require("node:path");
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];
  page.on("pageerror",e=>errors.push(String(e)));
  page.on("response",r=>{if(r.status()>=400)errors.push(r.status()+" "+r.url());});
  try{
    await page.goto("http://127.0.0.1:8767");
    await page.locator("#strategy-filter").selectOption("buy_disenchant");
    await page.locator("[data-opportunity]").first().click();
    await page.getByText("Ожидаемый выход распыления",{exact:false}).first().waitFor();
    assert.equal(await page.locator("[data-opportunity]").count(),1);
    await page.screenshot({path:path.join(process.argv[4],"disenchant-opportunity.png"),fullPage:true});
    await page.locator("#batches").fill("10");
    await page.getByRole("button",{name:"Рассчитать по объёму"}).click();
    await page.getByRole("cell",{name:"12",exact:true}).waitFor();
    await page.getByRole("cell",{name:"3",exact:true}).waitFor();
    await page.locator('[data-page="sources"]').click();
    await page.getByRole("heading",{name:"Справочник распыления"}).waitFor();
    const rules=await page.evaluate(async()=>await (await fetch('/api/v2/disenchant/rules/document')).json());
    rules.version="ui-reference-updated";
    await page.locator("#disenchant-file").setInputFiles({name:"updated.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify(rules))});
    await page.locator("#disenchant-preview").getByText("ui-reference-updated",{exact:false}).waitFor();
    await page.getByRole("button",{name:"Применить новую версию"}).click();
    await page.getByText("Справочник обновлён.",{exact:false}).waitFor();
    await page.getByText("Версия ui-reference-updated",{exact:false}).waitFor();
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      await page.screenshot({path:path.join(process.argv[4],`disenchant-reference-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"Page overflow at "+width);
    }
    await page.locator('nav [data-page="settings"]').click();
    await page.locator('[data-strategy="buy_disenchant"]').waitFor();
    assert(await page.locator('[data-strategy="craft_disenchant"]').isChecked());
    assert.deepEqual(errors,[]);
    console.log("Disenchant UI passed: expected outputs, reference import/preview, strategy settings, two widths, no browser errors");
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
