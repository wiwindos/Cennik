const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  try{
    await page.goto(process.argv[5]);
    await page.locator('[data-opportunity]').first().waitFor();
    await page.locator('nav [data-page="leveling"]').click();
    await page.locator('#leveling-form').waitFor();
    await page.locator('[name="profile_id"]').selectOption('b'.repeat(32));
    await page.getByText('Портняжное дело · 100/300',{exact:false}).waitFor();
    await page.locator('[name="crafts"]').fill('2');
    await page.getByRole('button',{name:'Подобрать варианты',exact:true}).click();
    await page.locator('[data-leveling-select="100:npc"]').waitFor();
    let result=await page.evaluate(()=>window.CennikLeveling.result);
    assert.equal(result.variants.length,2);
    assert.equal(result.variants.find(v=>v.exit==='npc').profit_copper,-40);
    await page.locator('[name="allow_disenchant"]').check();
    await page.getByRole('button',{name:'Подобрать варианты',exact:true}).click();
    await page.locator('[data-leveling-select="100:disenchant"]').waitFor();
    await page.locator('[data-leveling-select="100:npc"]').click();
    await page.locator('#leveling-detail').waitFor();
    assert.ok(await page.locator('#message').isHidden());
    assert.ok(await page.getByRole('button',{name:'Отправить выбранному крафтеру',exact:true}).isEnabled());
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`Overflow at ${width}`);
      await page.screenshot({path:path.join(process.argv[4],`leveling-${width}.png`),fullPage:true});
    }
    await page.locator('#leveling-max-loss').fill('0.0040');
    await page.getByRole('button',{name:'Отправить выбранному крафтеру',exact:true}).click();
    await page.waitForFunction(async()=>{const o=await(await fetch('/api/v2/overview')).json();return o.jobs.some(j=>j.kind==='profession_leveling'&&j.state==='completed');});
    const routes=await page.evaluate(async()=>await(await fetch('/fixture/tasks')).json());
    assert.equal(routes.auctioneer.length,0);assert.equal(routes.crafter.length,1,JSON.stringify(routes));
    const task=routes.crafter[0].tasks[0];
    assert.equal(task.strategy_id,'profession_leveling');assert.equal(task.min_profit_copper,-40);
    assert.equal(task.lines[0].quantity,6);assert.equal(task.opportunity.quantity,2);
    await page.locator('nav [data-page="production"]').click();
    await page.getByText('Крафтер сам закупает и изготавливает:',{exact:false}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('Profession UI passed: character snapshot → three exits → loss-approved batch → crafter-only purchase and production; desktop/mobile');
  }catch(error){await page.screenshot({path:path.join(process.argv[4],'failure.png'),fullPage:true});console.error(errors);console.error(await page.locator('#message').textContent());throw error;}
  finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
