const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  try{
    await page.goto(process.argv[5]);
    await page.getByRole('heading',{name:'Возможности',exact:true}).waitFor();
    await page.locator('nav [data-page="sources"]').click();
    await page.locator('#source-changes').waitFor();
    const table=page.locator('#source-changes table');
    await table.locator('tbody tr').filter({hasText:'Цены AHL'}).first().waitFor();
    const row=table.locator('tbody tr').filter({hasText:'Цены AHL'});
    await row.locator('summary').click();
    assert.match(await row.innerText(),/добавлено 1, изменено 1, исключено 1/);
    assert.match(await row.innerText(),/Примеры добавленных: #3/);
    assert.match(await row.innerText(),/API: без изменений \(304\)/);
    assert.match(await table.innerText(),/fixture-source-report/);
    assert.match(await page.locator('#upstream-settings').innerText(),/автообновление выключено; настроенный интервал 300 секунд/);
    await page.screenshot({path:path.join(process.argv[4],'source-changes-1440.png'),fullPage:true});
    await page.setViewportSize({width:390,height:1000});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(process.argv[4],'source-changes-390.png'),fullPage:true});
    await page.locator('nav [data-page="settings"]').click();
    await page.locator('#settings-form').waitFor();
    await page.locator('#source-changes table').waitFor();
    await page.locator('#ahl-token-form [name="token"]').fill('unsaved-fixture-token');
    await page.evaluate(async()=>{const r=await fetch('/fixture/source-error',{method:'POST'});if(!r.ok)throw Error(await r.text());});
    await page.waitForFunction(()=>document.getElementById('source-changes')?.textContent.includes('Тестовая ошибка AHL'),{timeout:15000});
    assert.equal(await page.locator('#ahl-token-form [name="token"]').inputValue(),'unsaved-fixture-token');
    const errorRow=page.locator('#source-changes tbody tr').filter({hasText:'Цены AHL'});
    await errorRow.locator('summary').click();
    assert.match(await errorRow.innerText(),/добавлено 1, изменено 1, исключено 1/);
    assert.deepEqual(errors,[]);
    console.log('Source reports UI passed: prices/FYI, counts and samples, 304, last successful change, live error refresh without resetting settings; desktop/mobile');
  }catch(error){await page.screenshot({path:path.join(process.argv[4],'failure.png'),fullPage:true});console.error(errors);console.error(await page.locator('#message').textContent());throw error;}
  finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
